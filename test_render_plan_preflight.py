from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_render_does_not_bump_override_revision_without_real_changes():
    source = (ROOT / "static" / "app.js").read_text()
    assert "await saveOverrides({ songIndexes: songs, reason: \"before-render\" });" not in source
    assert "async function prepareOverridesForRender()" in source
    assert 'await postOverrides("render-preflight")' in source


def test_batch_master_marks_only_changed_songs():
    source = (ROOT / "static" / "app.js").read_text()
    assert "const changed = current.mastering_intensity !== batchMaster" in source
    assert "if (changed) {" in source
    assert "markOverrideSequence(songId);" in source
