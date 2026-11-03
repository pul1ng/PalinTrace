import pytest

from palinreplay.replay.initial import initial_recorder
from palinreplay.replay.run import replay_range
from palinreplay.replay.session import run_replay


@pytest.mark.parametrize("entry,args,kwargs", [
    (run_replay, (None, None), {"output": None}),
    (initial_recorder, (None, None, None), {}),
    (replay_range, (None, None, None, None), {}),
])
@pytest.mark.parametrize("mode", ["strict", "lenient"])
def test_removed_library_keyword_is_rejected_before_side_effects(entry, args, kwargs, mode):
    with pytest.raises(TypeError, match="unexpected keyword argument 'validation_mode'"):
        entry(*args, **kwargs, validation_mode=mode)
