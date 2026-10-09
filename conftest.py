"""Always isolate test state before application modules are imported."""
import os
import tempfile

_test_state = tempfile.TemporaryDirectory(prefix='zuckermixer-tests-')
os.environ['ZUCKER_MIXER_STATE_ROOT'] = _test_state.name
