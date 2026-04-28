#!/usr/bin/env python3
"""
UEN Lookup API - Singapore Business/ACRA Registry Lookup
Deployed at: sg-uen-api.vercel.app
Price: $0.02 USDC per lookup

No other x402 API offers Singapore company/UEN lookup.
Uses ACRA BizFile+ public search.
"""

import re
import os
import json
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
import requests

app = FastAPI(title="Singapore UEN Lookup API", version="1.0.0")

# Price in USDC (wei)
PRICE_UEN = float(os.getenv("PRICE_UEN", "0.02"))

# x402 payment header
def require_payment(request: Request, price: float, path: str):
    """Verify x402 payment header is present."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        from x402_handler import create_payment_required_response
        return create_payment_required_response(price, path)
    return None

def create_payment_required_response(price_usdc: float, path: str) -> JSONResponse:
    """Return 402 Payment Required response."""
    price_wei = int(price_usdc * 1e18)
    return JSONResponse(
        status_code=402,
        content={
            "error": "Payment Required",
            "message": f"Pay {price_usdc} USDC to access {path}",
            "max_price": str(price_wei),
            "currency": "USDC",
            "network": "base",
            "payment_token": "Pay via x402 protocol",
        },
        headers={
            "WWW-Authenticate": f'x402 price="{price_wei}",n="USDC",p="base",g="{path}"'
        }
    )

# UEN format patterns
UEN_PATTERNS = {
    "local_company": re.compile(r"^\d{8}[A-Z]$"),
    "foreign_company": re.compile(r"^T\d{9}[A-Z]$"),
    "business": re.compile(r"^\d{9}[A-Z]$"),
    "society": re.compile(r"^S\d{8}$"),
}

def validate_uen(uen: str) -> tuple[bool, str]:
    uen = uen.strip().upper()
    for name, pattern in UEN_PATTERNS.items():
        if pattern.match(uen):
            type_names = {
                "local_company": "Local Company (ROC)",
                "foreign_company": "Foreign Company",
                "business": "Business / Sole Proprietorship",
                "society": "Society",
            }
            return True, type_names.get(name, name)
    return False, "Unknown"


@app.get("/uen/{uen}")
async def uen_lookup_get(request: Request, uen: str):
    """GET returns 402 Payment Required — must use POST."""
    err = require_payment(request, PRICE_UEN, f"/uen/{uen}")
    if err:
        return err
    raise HTTPException(status_code=405, detail="Use POST for UEN lookup")


@app.post("/uen/{uen}")
async def uen_lookup(request: Request, uen: str):
    """
    Singapore UEN / ACRA Business Lookup.
    Returns company details for any Singapore UEN (Unique Entity Number).
    
    POST /uen/{uen}
    Headers: Authorization: Bearer <x402_payment_token>
    
    Valid UENs:
    - Local Company: 197601155W (8 digits + 1 letter)
    - Foreign Company: T18Q123456A (T + 9 digits + 1 letter)
    - Business: 123456789A (9 digits + 1 letter)
    - Society: S1234567 (S + 7 digits)
    
    Price: $0.02 USDC on Base (eip155:8453)
    """
    err = require_payment(request, PRICE_UEN, f"/uen/{uen}")
    if err:
        return err

    uen = uen.strip().upper()
    
    # Validate format
    is_valid, uen_type = validate_uen(uen)
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"Invalid UEN format: '{uen}'. Expected: 197601155W (Local Co), T18Q123456A (Foreign), 123456789A (Business), S1234567 (Society)")
    
    # Look up via ACRA
    result = _lookup_acra(uen)
    result["price_charged"] = str(PRICE_UEN)
    result["fetched_at"] = datetime.now().isoformat()
    return result


def _lookup_acra(uen: str) -> dict:
    """Search ACRA BizFile+ for UEN."""
    ACRA_SEARCH = "https://www.acra.gov.sg/bizfilecom/SearchBiz!simplifiedSearch.action"
    
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; sg-uen-api/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.acra.gov.sg/",
        })
        
        resp = session.get(ACRA_SEARCH, params={"searchText": uen}, timeout=15)
        
        if resp.status_code != 200:
            return {"uen": uen, "valid": True, "uen_type": "Unknown", "error": f"ACRA unavailable ({resp.status_code})"}
        
        html = resp.text
        
        # Extract company name
        company_name = _extract(html, [
            r'Company Name\s*</td>\s*<td[^>]*>([^<]{3,100})',
            r'"companyName"\s*:\s*"([^"]+)"',
        ])
        
        if not company_name:
            return {"uen": uen, "valid": True, "found": False, "uen_type": _get_uen_type_desc(uen), "error": "UEN not found in ACRA — may be inactive or invalid"}
        
        return {
            "uen": uen,
            "valid": True,
            "found": True,
            "uen_type": _get_uen_type_desc(uen),
            "company_name": company_name,
            "entity_status": _extract(html, [r'Status\s*</td>\s*<td[^>]*>([^<]{3,50})', r'"status"\s*:\s*"([^"]+)"']),
            "registration_date": _extract(html, [r'Registration\s*Date\s*</td>\s*<td[^>]*>([^<]{5,30})']),
            "address": _extract(html, [r'Address\s*</td>\s*<td[^>]*>([^<]{10,200})']),
            "primary_ssic": _extract(html, [r'Primary SSIC\s*</td>\s*<td[^>]*>\s*\d{4}\s*[-–]\s*([^<]{5,100})']),
            "paid_up_capital": _extract(html, [r'Paid Up Capital\s*</td>\s*<td[^>]*>([^<]{3,50})']),
            "source": "ACRA BizFile+",
        }
        
    except requests.exceptions.Timeout:
        return {"uen": uen, "valid": True, "error": "ACRA lookup timed out — try again"}
    except Exception as e:
        return {"uen": uen, "valid": True, "error": f"Lookup failed: {str(e)}"}


def _extract(html: str, patterns: list) -> Optional[str]:
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val:
                return re.sub(r'\s+', ' ', val)
    return None


def _get_uen_type_desc(uen: str) -> str:
    if re.match(r'^\d{8}[A-Z]$', uen): return "Local Company (ROC)"
    if re.match(r'^T\d{9}[A-Z]$', uen): return "Foreign Company"
    if re.match(r'^\d{9}[A-Z]$', uen): return "Business / Sole Proprietorship"
    if re.match(r'^S\d{8}$', uen): return "Society"
    return "Unknown"


@app.get("/x402.json")
async def x402_manifest():
    """Serve x402 manifest for agent discovery."""
    import os
    manifest_path = os.path.join(os.path.dirname(__file__), 'x402.json')
    with open(manifest_path, 'r') as f:
        return JSONResponse(json.load(f))


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "Singapore UEN Lookup API",
        "version": "1.0.1",
        "price_per_lookup": f"${PRICE_UEN} USDC",
        "network": "Base (eip155:8453)",
    }


@app.get("/prices")
async def prices():
    return {
        "service": "Singapore UEN Lookup API",
        "version": "1.0.1",
        "endpoints": [
            {"method": "POST", "path": "/uen/{uen}", "price": f"${PRICE_UEN} USDC", "description": "Look up any Singapore UEN (company, business, or society) via ACRA BizFile+", "params": {"uen": "path param — 9 to 10 character UEN"}},
            {"method": "GET", "path": "/types", "price": "free", "description": "List all valid Singapore UEN formats with examples"},
        ],
        "network": "Base (eip155:8453)",
        "x402_enforced": True,
    }


@app.get("/types")
async def uen_types():
    """Free endpoint listing all valid Singapore UEN formats.
    
    Useful for agents to validate UENs before querying /uen/{uen}.
    No payment required.
    """
    return {
        "uen_types": [
            {
                "format": "12345678A",
                "pattern": r"^\\d{8}[A-Z]$",
                "example": "197601155W",
                "name": "Local Company (ROC)",
                "description": "Singapore registered companies under ACRA (Registry of Companies)",
                "issuing_authority": "ACRA",
            },
            {
                "format": "T123456789A",
                "pattern": r"^T\\d{9}[A-Z]$",
                "example": "T18Q123456A",
                "name": "Foreign Company",
                "description": "Overseas companies registered in Singapore as foreign entities",
                "issuing_authority": "ACRA",
            },
            {
                "format": "123456789A",
                "pattern": r"^\\d{9}[A-Z]$",
                "example": "123456789A",
                "name": "Business / Sole Proprietorship",
                "description": "Sole proprietorships and partnerships registered with ACRA",
                "issuing_authority": "ACRA",
            },
            {
                "format": "S1234567",
                "pattern": r"^S\\d{8}$",
                "example": "S1234567",
                "name": "Society",
                "description": "Societies, associations, and clubs registered under the Societies Act",
                "issuing_authority": "Registry of Societies (ROS)",
            },
        ],
        "note": "All UEN types can be looked up via POST /uen/{uen} for $0.02 USDC",
        "network": "Base (eip155:8453)",
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)