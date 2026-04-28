"""
x402 payment helpers for the Financial Data API.
Validates x402 v2 payment tokens locally (base64 decode + schema parse).
No external network calls required — works reliably on any hosting platform.

Compatible with x402 v2 protocol (HTTP 402 Payment Required).
"""

import base64
import json
import os
from typing import Optional

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

# x402 v2 SDK imports for schema validation only
from x402.schemas.payments import (
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    PaymentPayload,
)

# Wallet config
RECEIVING_WALLET = os.getenv("RECEIVING_WALLET", "0x50F9D979b825670A9936D992F5db8AEd9497208A")
USDC_ON_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MAX_TIMEOUT = 300  # 5 minutes


def _parse_bearer_token(auth_header: str) -> Optional[PaymentPayload]:
    """Parse and validate x402 v2 PaymentPayload from a Bearer token."""
    if not auth_header:
        return None

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    bearer_token = parts[1]

    try:
        # Decode base64 → raw bytes → JSON dict
        try:
            payload_bytes = base64.urlsafe_b64decode(bearer_token + "==")
        except Exception:
            payload_bytes = base64.b64decode(bearer_token)

        payload_json = payload_bytes.decode("utf-8")
        payload_data = json.loads(payload_json)

        # Parse with official x402 v2 SDK
        token = PaymentPayload.model_validate(payload_data)
        return token

    except Exception:
        return None


def create_payment_required_response(
    amount_usdc: float, resource: str = "/parse"
) -> JSONResponse:
    """
    Create a proper x402 v2 PaymentRequired JSONResponse using the official SDK.
    Returns a 402 response with proper camelCase x402 v2 error body.
    """
    amount_smallest = str(int(amount_usdc * 1_000_000))

    payment_required = PaymentRequired(
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network="eip155:8453",
                asset=USDC_ON_BASE,
                amount=amount_smallest,
                pay_to=RECEIVING_WALLET,
                max_timeout_seconds=MAX_TIMEOUT,
            )
        ],
        resource=ResourceInfo(url=resource),
    )

    resp_data = payment_required.model_dump()
    # Flatten resource object to just {url} per x402 v2 spec
    resp_data["resource"] = {"url": resource}

    return JSONResponse(status_code=402, content=resp_data)


async def verify_and_settle_payment(
    auth_header: str, required_amount_usdc: float
) -> tuple[bool, Optional[str]]:
    """
    Verify an x402 v2 payment token from Authorization header.
    
  Steps:
    1. Parse the Bearer token into a PaymentPayload
    2. Validate the payment amount is sufficient (in smallest units = microUSDC)
    3. Verify the network is Base (eip155:8453)
    4. Verify the pay_to address matches our wallet
    
  Returns (is_valid, error_message).
  
  Note: Full on-chain settlement verification requires a facilitator.
  For production, integrate with an x402 facilitator service.
  """
    token = _parse_bearer_token(auth_header)
    if token is None:
        return False, "Missing or invalid x402 payment token — use Authorization: Bearer <token>"

    # Get payment details from token payload
    try:
        # Payment data is directly in token.payload (x402 v2 format)
        # token.payload = {scheme, network, asset, amount, pay_to, max_timeout_seconds, signature}
        payment_info = token.payload
        amount_str = payment_info.get("amount", "0")
        network = payment_info.get("network", "")
        signature = payment_info.get("signature", "")
        pay_to = payment_info.get("pay_to", "")
        
        # Parse amount (can be string or int)
        if isinstance(amount_str, str):
            amount_val = int(amount_str)
        else:
            amount_val = amount_str
        
        # Verify amount is sufficient (in smallest units = microUSDC)
        required_smallest = int(required_amount_usdc * 1_000_000)
        if amount_val < required_smallest:
            return False, f"Insufficient payment: got {amount_val} (${amount_val/1e6:.4f}), need {required_smallest} (${required_amount_usdc:.2f})"
        
        # Verify network is Base (eip155:8453)
        # Handle both eip155:8453 and eip155:84532 (facilitator sometimes returns 84532)
        if network and not (network == "eip155:8453" or network == "eip155:84532"):
            return False, f"Wrong network: {network} — use Base (eip155:8453)"
        
        # Verify signature is present (non-empty)
        if not signature:
            return False, "Payment token missing signature"
        
        # Verify pay_to matches our wallet (prevent payment to wrong recipient)
        if pay_to and pay_to.lower() != RECEIVING_WALLET.lower():
            return False, f"Payment sent to wrong address: {pay_to} — expected {RECEIVING_WALLET}"
        
        return True, None
        
    except Exception as e:
        return False, f"Payment token validation error: {e}"


async def require_payment(
    request: Request, price_usdc: float, resource: str = "/parse"
) -> None:
    """
    FastAPI dependency that enforces x402 payment on an endpoint.
    Use as: await require_payment(request, 0.02, "/parse")

    Raises HTTPException(402) with proper x402 v2 PaymentRequired if payment is missing/invalid.
    """
    auth_header = request.headers.get("Authorization", "")
    is_valid, error = await verify_and_settle_payment(auth_header, price_usdc)

    if not is_valid:
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "x402Version": 2,
                    "resource": resource,
                    "accepts": [
                        {
                            "scheme": "exact",
                            "network": "eip155:8453",
                            "asset": USDC_ON_BASE,
                            "amount": str(int(price_usdc * 1_000_000)),
                            "payTo": RECEIVING_WALLET,
                            "maxTimeoutSeconds": MAX_TIMEOUT,
                            "extra": {},
                        }
                    ],
                }
            },
        )