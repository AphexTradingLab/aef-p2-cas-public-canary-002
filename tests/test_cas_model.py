import unittest

from scripts.cas_model import (
    canonical_bytes,
    seed_state,
    state_digest,
    successor_state,
    validate_seed,
    validate_successor,
)


class SyntheticCasModelTests(unittest.TestCase):
    def test_canonical_digest_is_stable(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        self.assertEqual(state_digest(seed_state()), state_digest(seed_state()))

    def test_seed_has_no_authority(self):
        seed = seed_state()
        validate_seed(seed)
        self.assertEqual(seed["authority"], "NONE")
        self.assertIs(seed["execution_authorized"], False)
        self.assertEqual(seed["dispatch_status"], "NONE")
        self.assertIs(seed["outbox_write"], False)
        self.assertIsNone(seed["continuation_decision"])

    def test_sibling_successors_are_valid_and_distinct(self):
        seed = seed_state()
        a = successor_state(seed, "123456", "writer-a")
        b = successor_state(seed, "123456", "writer-b")
        validate_successor(seed, a)
        validate_successor(seed, b)
        self.assertNotEqual(state_digest(a), state_digest(b))
        self.assertEqual(a["previous_state_sha256"], b["previous_state_sha256"])

    def test_mutation_and_scope_expansion_are_rejected(self):
        seed = seed_state()
        candidate = successor_state(seed, "123456", "writer-a")
        candidate["execution_authorized"] = True
        with self.assertRaisesRegex(ValueError, "execution_authorized"):
            validate_successor(seed, candidate)

    def test_wrong_parent_digest_is_rejected(self):
        seed = seed_state()
        candidate = successor_state(seed, "123456", "writer-a")
        candidate["previous_state_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "parent digest"):
            validate_successor(seed, candidate)

    def test_unknown_field_is_rejected(self):
        seed = seed_state()
        candidate = successor_state(seed, "123456", "writer-a")
        candidate["extra"] = "not allowed"
        with self.assertRaisesRegex(ValueError, "closed schema"):
            validate_successor(seed, candidate)

    def test_unknown_writer_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown writer"):
            successor_state(seed_state(), "123456", "writer-c")


if __name__ == "__main__":
    unittest.main()
