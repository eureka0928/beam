"""
Merkle tree implementation for payment proofs.

Re-exports from shared module for backwards compatibility.
"""

from neurons.shared.merkle import (
    hash_leaf,
    hash_pair,
    MerkleTree,
    create_payment_merkle_tree,
    verify_payment_inclusion,
)

__all__ = [
    "hash_leaf",
    "hash_pair",
    "MerkleTree",
    "create_payment_merkle_tree",
    "verify_payment_inclusion",
]
