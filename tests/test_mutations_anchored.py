"""Every mutation in the harness must still find its anchor.

The harness treats a missing anchor as an error, but only when it is run,
and it is run by hand. A refactor that moves the anchored line otherwise
retires the check unnoticed: the agent-mode empty-response mutation sat
unanchored for a day after its line was renamed.
"""

import pytest

from tests.mutations import MUTATIONS, ROOT


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.name)
def test_mutation_anchor_exists(mutation):
    source = (ROOT / mutation.path).read_text()
    assert mutation.old in source, (
        f"{mutation.name}: anchor not found in {mutation.path}; "
        "update or retire the mutation"
    )
    assert source.count(mutation.old) == 1, (
        f"{mutation.name}: anchor matches more than once in {mutation.path}"
    )
