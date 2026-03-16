"""
On-Chain Transaction Verifier

Verifies orchestrator payment tx_hashes against blockchain records.
Used by validators to ensure orchestrators actually paid workers on-chain.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class TxVerificationResult:
    """Result of verifying a transaction on-chain."""
    is_valid: bool
    error: Optional[str] = None
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    amount_tao: Optional[float] = None


class TxVerifier:
    """
    Verifies transfer transactions on the Bittensor chain.

    Checks that:
    1. tx_hash exists and is a valid transfer
    2. Sender matches expected orchestrator coldkey
    3. Recipient matches expected worker address
    4. Amount matches expected payment (within tolerance)
    """

    def __init__(self, subtensor):
        """
        Initialize verifier with subtensor connection.

        Args:
            subtensor: Bittensor subtensor instance
        """
        self.subtensor = subtensor
        self._cache: Dict[str, TxVerificationResult] = {}

    def verify_transfer(
        self,
        tx_hash: str,
        expected_from: str,
        expected_to: str,
        expected_amount: float,
        tolerance: float = 0.01,
    ) -> TxVerificationResult:
        """
        Verify a transfer transaction on-chain.

        Args:
            tx_hash: Transaction hash in format "{extrinsic_hash}:{block_hash}"
            expected_from: Expected sender address (orchestrator coldkey)
            expected_to: Expected recipient address (worker payment address)
            expected_amount: Expected TAO amount
            tolerance: Allowed amount deviation (default 1%)

        Returns:
            TxVerificationResult with verification status
        """
        # Check cache first
        if tx_hash in self._cache:
            return self._cache[tx_hash]

        # Query chain for extrinsic
        result = self._query_extrinsic(tx_hash)

        if not result.is_valid:
            self._cache[tx_hash] = result
            return result

        # Validate sender
        if result.from_address != expected_from:
            result = TxVerificationResult(
                is_valid=False,
                error=f"sender mismatch: expected {expected_from[:16]}..., got {result.from_address[:16] if result.from_address else 'None'}...",
                from_address=result.from_address,
                to_address=result.to_address,
                amount_tao=result.amount_tao,
            )
            self._cache[tx_hash] = result
            return result

        # Validate recipient
        if result.to_address != expected_to:
            result = TxVerificationResult(
                is_valid=False,
                error=f"recipient mismatch: expected {expected_to[:16]}..., got {result.to_address[:16] if result.to_address else 'None'}...",
                from_address=result.from_address,
                to_address=result.to_address,
                amount_tao=result.amount_tao,
            )
            self._cache[tx_hash] = result
            return result

        # Validate amount (within tolerance)
        if result.amount_tao is not None and expected_amount > 0:
            deviation = abs(result.amount_tao - expected_amount) / expected_amount
            if deviation > tolerance:
                result = TxVerificationResult(
                    is_valid=False,
                    error=f"amount mismatch: expected {expected_amount:.6f} TAO, got {result.amount_tao:.6f} TAO (deviation: {deviation:.2%})",
                    from_address=result.from_address,
                    to_address=result.to_address,
                    amount_tao=result.amount_tao,
                )
                self._cache[tx_hash] = result
                return result

        # All checks passed
        self._cache[tx_hash] = result
        return result

    def _query_extrinsic(self, tx_hash: str) -> TxVerificationResult:
        """
        Query the blockchain for extrinsic details.

        Args:
            tx_hash: Either "{extrinsic_hash}:{block_hash}" (new format) or
                     just "{extrinsic_hash}" (legacy format)

        Returns:
            TxVerificationResult with transfer details or error
        """
        try:
            # Get substrate interface
            substrate = self.subtensor.substrate

            # Parse tx_hash - new format includes block_hash
            # Format: {extrinsic_hash}:{block_hash}
            extrinsic_hash = tx_hash
            block_hash = None

            if ":" in tx_hash and tx_hash.count(":") == 1:
                parts = tx_hash.split(":")
                # Both parts should start with 0x for valid hashes
                if parts[0].startswith("0x") and parts[1].startswith("0x"):
                    extrinsic_hash = parts[0]
                    block_hash = parts[1]

            if not block_hash:
                # Legacy format - can't verify without block_hash
                return TxVerificationResult(
                    is_valid=False,
                    error=f"missing block_hash in tx_hash (legacy format): {tx_hash[:20]}...",
                )

            # Get the block and find the extrinsic by hash
            block = substrate.get_block(block_hash=block_hash)
            if not block:
                return TxVerificationResult(
                    is_valid=False,
                    error=f"block not found: {block_hash[:20]}...",
                )

            # Find extrinsic by hash in block
            extrinsic_data = None
            for ext in block.get("extrinsics", []):
                ext_hash_raw = ext.extrinsic_hash if hasattr(ext, "extrinsic_hash") else None
                if ext_hash_raw:
                    # Convert bytes to hex string for comparison
                    ext_hash_str = "0x" + ext_hash_raw.hex() if isinstance(ext_hash_raw, bytes) else str(ext_hash_raw)
                    if ext_hash_str.lower() == extrinsic_hash.lower():
                        # Get the value dict from the extrinsic
                        extrinsic_data = ext.value if hasattr(ext, "value") else None
                        break

            if not extrinsic_data:
                return TxVerificationResult(
                    is_valid=False,
                    error=f"extrinsic not found in block: {extrinsic_hash[:20]}...",
                )

            # Extract transfer details from extrinsic
            # Look for Balances.transfer or Balances.transfer_keep_alive call
            call = extrinsic_data.get("call", {})
            call_module = call.get("call_module", "")
            call_function = call.get("call_function", "")

            if call_module != "Balances" or "transfer" not in call_function.lower():
                return TxVerificationResult(
                    is_valid=False,
                    error=f"not a transfer: {call_module}.{call_function}",
                )

            # Extract sender (from address field in extrinsic)
            sender = None
            if "address" in extrinsic_data:
                sender = extrinsic_data["address"]
            elif "signature" in extrinsic_data:
                sig_data = extrinsic_data["signature"]
                if isinstance(sig_data, dict) and "address" in sig_data:
                    sender = sig_data["address"]

            # Extract recipient and amount from call args
            call_args = call.get("call_args", [])
            recipient = None
            amount_raw = None

            for arg in call_args:
                if arg.get("name") in ["dest", "destination"]:
                    dest_value = arg.get("value", {})
                    if isinstance(dest_value, dict):
                        recipient = dest_value.get("Id") or dest_value.get("id")
                    elif isinstance(dest_value, str):
                        recipient = dest_value
                elif arg.get("name") in ["value", "amount"]:
                    amount_raw = arg.get("value", 0)

            # Convert amount from raw (rao) to TAO
            amount_tao = None
            if amount_raw is not None:
                amount_tao = float(amount_raw) / 1e9  # 1 TAO = 1e9 rao

            if not sender or not recipient:
                return TxVerificationResult(
                    is_valid=False,
                    error=f"could not parse transfer details from extrinsic",
                )

            return TxVerificationResult(
                is_valid=True,
                from_address=sender,
                to_address=recipient,
                amount_tao=amount_tao,
            )

        except Exception as e:
            logger.error(f"Error querying extrinsic {tx_hash[:20]}...: {e}")
            return TxVerificationResult(
                is_valid=False,
                error=f"query error: {str(e)[:100]}",
            )

    def clear_cache(self):
        """Clear the verification cache."""
        self._cache.clear()

    def get_cache_stats(self) -> Dict:
        """Get cache statistics."""
        valid = sum(1 for r in self._cache.values() if r.is_valid)
        invalid = len(self._cache) - valid
        return {
            "total": len(self._cache),
            "valid": valid,
            "invalid": invalid,
        }
