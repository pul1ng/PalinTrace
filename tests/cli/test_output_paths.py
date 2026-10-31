from pathlib import Path

import palinreplay.cli.app as cli
import palinreplay.cli.outputs as outputs
from palinreplay.tasks import DatasetSource


def test_default_download_and_conversion_publish_in_typed_directories(
    tmp_path, monkeypatch, row, capsys,
):
    from palinreplay.tasks.sources.swebench import common as sb

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261031_131022")
    monkeypatch.setattr(sb, "fetch", lambda dataset, split, revision, **kwargs: (
        [row], DatasetSource(source="swebench_lite", dataset=dataset, split=split),
    ))
    assert cli.main(["download", "dataset", "--source", "swebench_lite"]) == 0
    raw = tmp_path / "outputs/download/261031_131022_swebench_lite"
    assert (raw / "fetch.json").is_file()
    assert cli.main(["convert", "--input", str(raw)]) == 0
    converted = tmp_path / "outputs/convert/261031_131022_swebench_lite"
    assert (converted / "manifest.json").is_file()
    assert str(raw) in capsys.readouterr().out
    before = (converted / "manifest.json").read_bytes()
    assert cli.main(["convert", "--input", str(raw)]) == 1
    assert (converted / "manifest.json").read_bytes() == before


def test_default_names_strip_old_timestamps_and_keep_one_path_component(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(outputs, "timestamp", lambda: "261031_131022")
    assert outputs.default_output("visual", "261030_123456_astropy", suffix=".html") == (
        tmp_path / "outputs/visual/261031_131022_astropy.html"
    )
    assert outputs.default_output("convert", "SWE-bench/Lite").name == "261031_131022_SWE-bench_Lite"
    assert not (tmp_path / "outputs").exists()


def test_invalid_fetch_metadata_fails_without_creating_default_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "fetch.json").write_text('{"source": []}')
    assert cli.main(["convert", "--input", str(raw)]) == 1
    assert not (tmp_path / "outputs").exists()


def test_explicit_conversion_output_is_not_renamed(tmp_path, monkeypatch, row):
    import json

    monkeypatch.chdir(tmp_path)
    source = tmp_path / "rows.json"
    source.write_text(json.dumps([row]))
    assert cli.main([
        "convert", "--source", "swebench_lite", "--input", str(source),
        "--output", "my-tasks",
    ]) == 0
    assert Path("my-tasks/manifest.json").is_file()
    assert not Path("outputs").exists()
