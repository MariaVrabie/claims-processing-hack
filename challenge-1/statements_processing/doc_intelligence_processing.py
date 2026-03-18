# Import Required Libraries
import os
import json
import re
from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from collections import defaultdict

# Load environment variables
load_dotenv()

# Statements files location
STATEMENTS_IMAGE_FOLDER = '../../challenge-0/data/statements/'
STATEMENTS_OUTPUT_LOCATION = '../output/doc_intelligence/'

# Azure Document Intelligence credentials
DOCUMENT_INTELLIGENCE_ENDPOINT = os.getenv('DOCUMENT_INTELLIGENCE_ENDPOINT')
DOCUMENT_INTELLIGENCE_KEY = os.getenv('DOCUMENT_INTELLIGENCE_KEY')

# Initialize Document Intelligence Client
doc_intelligence_client = DocumentIntelligenceClient(
    endpoint=DOCUMENT_INTELLIGENCE_ENDPOINT,
    credential=AzureKeyCredential(DOCUMENT_INTELLIGENCE_KEY)
)

print(f"✅ Configuration loaded:")
print(f"   Document Intelligence Endpoint: {DOCUMENT_INTELLIGENCE_ENDPOINT}")


# =============================================================================
# Field Mappings (same as mistral_doc_intel_annotations.py)
# =============================================================================

FIELD_MAPPINGS = {
    "claimant_name": ["Name:", "Policyholder Name:", "Claimant Name:"],
    "claim_date": ["Date of Incident:", "Claim Date:", "Accident Date:"],
    "policy_number": ["Policy Number:", "Policy No:", "Policy #:"],
    "damage_description": ["Damage Description:", "Damages:"],
    "estimated_damage_amount": ["Estimated Damage:", "Damage Amount:", "Estimated Cost:", "Amount:"],
    "signature_present": ["Signature:", "Signed:"],
    "date_signed": ["Date Signed:", "Signature Date:"],
}

VEHICLE_FIELD_MAPPINGS = {
    "make": ["Make:", "Vehicle Make:"],
    "model": ["Model:", "Vehicle Model:"],
    "year": ["Year:", "Vehicle Year:"],
    "license_plate": ["License Plate:", "Plate:", "License:"],
    "vin": ["VIN:", "Vehicle Identification Number:"],
}

SECTION_HEADERS = [
    "Policyholder Information",
    "Vehicle Information",
    "Accident Information",
    "Description of Incident",
    "Damage Assessment",
    "Other Party Information",
    "Witness Information",
    "Police Report",
    "Signature",
    "Additional Notes",
]


# =============================================================================
# Utility Functions
# =============================================================================

def read_file_bytes(file_path):
    with open(file_path, "rb") as f:
        return f.read()


def get_content_type(file_path):
    ext = file_path.lower()
    if ext.endswith('.png'):
        return 'image/png'
    elif ext.endswith(('.jpg', '.jpeg')):
        return 'image/jpeg'
    elif ext.endswith('.pdf'):
        return 'application/pdf'
    return 'application/octet-stream'


# =============================================================================
# Structured Parsing (matching mistral_doc_intel_annotations.py field mappings)
# =============================================================================

def merge_label_value_lines(raw_text):
    """
    Document Intelligence often puts labels and values on separate lines.
    This merges 'Label:\\n Value' into 'Label: Value' for easier parsing.
    """
    lines = raw_text.split('\n')
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Check if this line looks like a label (ends with colon or is a known label)
        is_label = line.endswith(':') or any(
            line.lower().startswith(p.lower().rstrip(':'))
            for mappings in [FIELD_MAPPINGS, VEHICLE_FIELD_MAPPINGS]
            for patterns in mappings.values()
            for p in patterns
        )
        if is_label and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            # If next line is non-empty and not itself a label/header, merge
            is_next_label = next_line.endswith(':') or next_line in SECTION_HEADERS
            if next_line and not is_next_label:
                if line.endswith(':'):
                    merged.append(f"{line} {next_line}")
                else:
                    merged.append(f"{line}: {next_line}")
                i += 2
                continue
        merged.append(line)
        i += 1
    return merged


def parse_to_structured_data(raw_text):
    """
    Parse raw Document Intelligence text into structured fields,
    using the same field mappings as mistral_doc_intel_annotations.py.
    """
    lines = merge_label_value_lines(raw_text)
    extracted = {}

    # Extract simple key-value pairs
    for line in lines:
        if not line or line in SECTION_HEADERS:
            continue
        for field_name, patterns in FIELD_MAPPINGS.items():
            for pattern in patterns:
                if pattern.lower() in line.lower():
                    idx = line.lower().find(pattern.lower())
                    value = line[idx + len(pattern):].strip()
                    if value:
                        extracted[field_name] = value
                    break

    # Extract vehicle info (including combined Year/Make/Model)
    vehicle_info = {}
    for line in lines:
        if not line:
            continue
        if "Year/Make/Model:" in line:
            value = line.split(":", 1)[1].strip()
            parts = value.split()
            if len(parts) >= 3:
                vehicle_info["year"] = parts[0]
                vehicle_info["make"] = parts[1]
                vehicle_info["model"] = " ".join(parts[2:])
            elif len(parts) == 2:
                vehicle_info["year"] = parts[0]
                vehicle_info["make"] = parts[1]
            continue
        for field_name, patterns in VEHICLE_FIELD_MAPPINGS.items():
            for pattern in patterns:
                if pattern.lower() in line.lower():
                    idx = line.lower().find(pattern.lower())
                    value = line[idx + len(pattern):].strip()
                    if value:
                        vehicle_info[field_name] = value
                    break
    if vehicle_info:
        extracted["vehicle_info"] = vehicle_info

    # Extract additional fields that appear as label:value on the merged lines
    extra_mappings = {
        "address": ["Address:"],
        "phone": ["Phone:"],
        "email": ["Email:"],
        "color": ["Color:"],
        "time": ["Time:"],
        "location": ["Location:"],
        "claimant_id": ["Claimant ID:", "Claimant Id:"],
    }
    for field_name, patterns in extra_mappings.items():
        for line in lines:
            if not line:
                continue
            for pattern in patterns:
                if pattern.lower() in line.lower():
                    idx = line.lower().find(pattern.lower())
                    value = line[idx + len(pattern):].strip()
                    if value:
                        extracted[field_name] = value
                    break

    # Extract incident description (multi-line paragraph after header)
    in_description = False
    description_lines = []
    for line in lines:
        if any(h in line for h in ["Description of Incident", "Description of the Incident",
                                    "Incident Description", "Description"]) and not line.startswith("Damage"):
            in_description = True
            # If there's text after the header on the same line
            after = line.split(":", 1)[1].strip() if ":" in line else ""
            if after:
                description_lines.append(after)
            continue
        if in_description:
            # Stop at next section header
            if line in SECTION_HEADERS or (line and not line[0].isspace() and line.endswith(':')):
                is_section = any(h.lower() in line.lower() for h in SECTION_HEADERS)
                if is_section:
                    break
            if line.strip():
                description_lines.append(line.strip())
    if description_lines:
        extracted["incident_description"] = " ".join(description_lines)

    # Check for signature presence
    signature_keywords = ["signature", "signed", "sign here"]
    has_signature = any(kw in raw_text.lower() for kw in signature_keywords)
    extracted["signature_present"] = has_signature

    return extracted


def build_confidence_map(result):
    """
    Build a mapping from line content to average word confidence score
    using the Document Intelligence word-level confidence data.
    """
    confidence_map = {}

    if result.get("pages"):
        for page in result["pages"]:
            # Map line content to confidence from line-level data
            for line in page.get("lines", []):
                content = line.get("content", "").strip()
                if content and line.get("confidence") is not None:
                    confidence_map[content] = line["confidence"]

            # Also build word-level average for merged lines
            if page.get("words"):
                for word in page["words"]:
                    content = word.get("content", "").strip()
                    if content and word.get("confidence") is not None:
                        confidence_map[content] = word["confidence"]

    # Add key-value pair confidence
    for kvp in result.get("key_value_pairs", []):
        key = kvp.get("key", "").strip()
        value = kvp.get("value", "").strip()
        conf = kvp.get("confidence")
        if value and conf is not None:
            confidence_map[value] = conf
        if key and value and conf is not None:
            confidence_map[f"{key}: {value}"] = conf

    return confidence_map


def lookup_confidence(confidence_map, value):
    """Look up the confidence score for a value string."""
    if not value:
        return None
    value = value.strip()
    # Direct match
    if value in confidence_map:
        return confidence_map[value]
    # Try matching individual words and averaging
    words = value.split()
    scores = [confidence_map[w] for w in words if w in confidence_map]
    if scores:
        return sum(scores) / len(scores)
    return None


def fmt_conf(score):
    """Format a confidence score for markdown display."""
    if score is None:
        return ""
    return f" *(confidence: {score:.2f})*"


def format_as_structured_markdown(extracted, raw_text, confidence_map=None):
    """
    Format the extracted structured data as clean markdown,
    matching the Mistral output style, with Document Intelligence confidence scores.
    """
    if confidence_map is None:
        confidence_map = {}
    md_lines = []

    def _conf(value):
        return fmt_conf(lookup_confidence(confidence_map, value))

    # Title
    if "ACCIDENT STATEMENT" in raw_text.upper():
        md_lines.append("# ACCIDENT STATEMENT FORM\n")
    else:
        md_lines.append("# DOCUMENT EXTRACT\n")

    # Policyholder Information
    has_policyholder = any(k in extracted for k in ["claimant_name", "address", "phone", "email", "policy_number", "claimant_id"])
    if has_policyholder:
        md_lines.append("## Policyholder Information\n")
        if "claimant_name" in extracted:
            md_lines.append(f"Name: {extracted['claimant_name']}{_conf(extracted['claimant_name'])}")
        if "address" in extracted:
            md_lines.append(f"Address: {extracted['address']}{_conf(extracted['address'])}")
        if "phone" in extracted:
            md_lines.append(f"Phone: {extracted['phone']}{_conf(extracted['phone'])}")
        if "email" in extracted:
            md_lines.append(f"Email: {extracted['email']}{_conf(extracted['email'])}")
        if "policy_number" in extracted:
            md_lines.append(f"Policy Number: {extracted['policy_number']}{_conf(extracted['policy_number'])}")
        if "claimant_id" in extracted:
            md_lines.append(f"Claimant ID: {extracted['claimant_id']}{_conf(extracted['claimant_id'])}")
        md_lines.append("")

    # Vehicle Information
    vehicle = extracted.get("vehicle_info", {})
    has_vehicle = vehicle or "color" in extracted
    if has_vehicle:
        md_lines.append("## Vehicle Information\n")
        if vehicle.get("year") and vehicle.get("make") and vehicle.get("model"):
            ymm = f"{vehicle['year']} {vehicle['make']} {vehicle['model']}"
            md_lines.append(f"Year/Make/Model: {ymm}{_conf(ymm)}")
        else:
            for k in ["year", "make", "model"]:
                if vehicle.get(k):
                    md_lines.append(f"{k.title()}: {vehicle[k]}{_conf(vehicle[k])}")
        if "color" in extracted:
            md_lines.append(f"Color: {extracted['color']}{_conf(extracted['color'])}")
        if vehicle.get("vin"):
            md_lines.append(f"VIN: {vehicle['vin']}{_conf(vehicle['vin'])}")
        if vehicle.get("license_plate"):
            md_lines.append(f"License Plate: {vehicle['license_plate']}{_conf(vehicle['license_plate'])}")
        md_lines.append("")

    # Accident Information
    has_accident = any(k in extracted for k in ["claim_date", "time", "location"])
    if has_accident:
        md_lines.append("## Accident Information\n")
        if "claim_date" in extracted:
            md_lines.append(f"Date of Incident: {extracted['claim_date']}{_conf(extracted['claim_date'])}")
        if "time" in extracted:
            md_lines.append(f"Time: {extracted['time']}{_conf(extracted['time'])}")
        if "location" in extracted:
            md_lines.append(f"Location: {extracted['location']}{_conf(extracted['location'])}")
        md_lines.append("")

    # Description of Incident
    if "incident_description" in extracted:
        md_lines.append("## Description of Incident\n")
        md_lines.append(extracted["incident_description"])
        md_lines.append("")

    # Damage info
    if "damage_description" in extracted:
        md_lines.append("## Damage Assessment\n")
        md_lines.append(extracted["damage_description"])
        if "estimated_damage_amount" in extracted:
            md_lines.append(f"\nEstimated Damage: {extracted['estimated_damage_amount']}{_conf(extracted['estimated_damage_amount'])}")
        md_lines.append("")

    # Signature
    if extracted.get("signature_present"):
        md_lines.append("## Signature\n")
        md_lines.append("Signature: Present")
        if "date_signed" in extracted:
            md_lines.append(f"Date Signed: {extracted['date_signed']}{_conf(extracted['date_signed'])}")
        md_lines.append("")

    # Overall confidence summary
    all_scores = [v for v in confidence_map.values() if v is not None]
    if all_scores:
        avg = sum(all_scores) / len(all_scores)
        md_lines.append("---")
        md_lines.append(f"*Overall Document Intelligence confidence: {avg:.2f} (from {len(all_scores)} scored elements)*\n")

    return "\n".join(md_lines)


# =============================================================================
# Document Intelligence Analysis
# =============================================================================

def analyze_document(file_path):
    """Analyze a single document image with Document Intelligence prebuilt-layout model."""
    file_bytes = read_file_bytes(file_path)
    content_type = get_content_type(file_path)

    poller = doc_intelligence_client.begin_analyze_document(
        model_id="prebuilt-layout",
        body=file_bytes,
        content_type=content_type,
    )
    result = poller.result()

    raw_content = result.content if result.content else ""

    # Build structured output with confidence scores
    output = {
        "content": raw_content,
        "pages": [],
        "tables": [],
        "key_value_pairs": []
    }

    if result.pages:
        for page in result.pages:
            page_info = {
                "page_number": page.page_number,
                "width": page.width,
                "height": page.height,
                "lines": []
            }
            if page.lines:
                for line in page.lines:
                    line_info = {
                        "content": line.content,
                        "confidence": line.confidence if hasattr(line, 'confidence') and line.confidence is not None else None
                    }
                    page_info["lines"].append(line_info)
            if page.words:
                page_info["words"] = []
                for word in page.words:
                    page_info["words"].append({
                        "content": word.content,
                        "confidence": word.confidence
                    })
            output["pages"].append(page_info)

    if result.tables:
        for table in result.tables:
            table_info = {
                "row_count": table.row_count,
                "column_count": table.column_count,
                "cells": []
            }
            if table.cells:
                for cell in table.cells:
                    table_info["cells"].append({
                        "row_index": cell.row_index,
                        "column_index": cell.column_index,
                        "content": cell.content,
                        "confidence": cell.confidence if hasattr(cell, 'confidence') and cell.confidence is not None else None
                    })
            output["tables"].append(table_info)

    if result.key_value_pairs:
        for kvp in result.key_value_pairs:
            key = kvp.key.content if kvp.key else ""
            value = kvp.value.content if kvp.value else ""
            output["key_value_pairs"].append({
                "key": key,
                "value": value,
                "confidence": kvp.confidence
            })

    return output


# =============================================================================
# Main Processing
# =============================================================================

def process_statements_with_doc_intelligence():
    """Process all statement images from local folder using Azure Document Intelligence."""

    image_files = [
        f
        for f in os.listdir(STATEMENTS_IMAGE_FOLDER)
        if f.lower().endswith((".jpeg", ".jpg", ".png"))
    ]

    os.makedirs(STATEMENTS_OUTPUT_LOCATION, exist_ok=True)

    for image in image_files:
        print(f"Processing {image} with Document Intelligence...")
        image_path = os.path.join(STATEMENTS_IMAGE_FOLDER, image)
        result = analyze_document(image_path)

        # Parse raw content into structured fields using the field mappings
        extracted = parse_to_structured_data(result["content"])

        # Build confidence map from Document Intelligence word/line data
        confidence_map = build_confidence_map(result)

        # Format as clean structured markdown with confidence scores
        structured_md = format_as_structured_markdown(extracted, result["content"], confidence_map)

        md_name = os.path.splitext(image)[0] + ".md"
        md_path = os.path.join(STATEMENTS_OUTPUT_LOCATION, md_name)
        with open(md_path, "w", encoding="utf-8") as md_file:
            md_file.write(structured_md)

        # Also save the structured JSON alongside the markdown
        json_name = os.path.splitext(image)[0] + ".json"
        json_path = os.path.join(STATEMENTS_OUTPUT_LOCATION, json_name)
        with open(json_path, "w", encoding="utf-8") as json_file:
            json.dump({
                "extracted_fields": extracted,
                "confidence_scores": {k: lookup_confidence(confidence_map, v) for k, v in extracted.items() if isinstance(v, str)},
                "raw_content": result["content"],
                "key_value_pairs": result["key_value_pairs"],
                "tables": result["tables"]
            }, json_file, indent=2, ensure_ascii=False)

        print(f"💾 Markdown saved to {md_path}")
        print(f"💾 JSON saved to {json_path}")

    print(f"\n✅ Processed {len(image_files)} images with Document Intelligence")

    return image_files


if __name__ == "__main__":
    results = process_statements_with_doc_intelligence()