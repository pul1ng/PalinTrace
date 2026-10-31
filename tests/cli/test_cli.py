import json
from pathlib import Path

import pytest

import palinreplay.cli.app as cli
from palinreplay.tasks import DatasetSource, load_dataset, load_fetch


def test_cli_local_conversion_and_inspection(tmp_path, row, capsys):
    source = tmp_path / "input.json"
    source.write_text(json.dumps([row]))
    output = tmp_path / "out"
    assert (
        cli.main(
            [
                "convert",
                "--source",
                "swebench_lite",
                "--input",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "no image was pulled" in capsys.readouterr().out
    assert cli.main(["inspect", str(output / row["instance_id"]), "--agent-input"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["instruction"] == row["problem_statement"]
    assert "evaluation" not in data
    assert row["patch"] not in json.dumps(data)

    assert cli.main(["inspect", str(output / row["instance_id"] / "task.json")]) == 0
    assert "evaluation" in json.loads(capsys.readouterr().out)


def test_cli_reports_error_without_partial_output(tmp_path, capsys):
    source = tmp_path / "input.json"
    source.write_text('[{"instance_id":"../escape"}]')
    output = tmp_path / "out"
    assert (
        cli.main(
            [
                "convert",
                "--source",
                "swebench_lite",
                "--input",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


def test_cli_unknown_source_lists_available(tmp_path, capsys):
    source = tmp_path / "input.json"
    source.write_text("[]")
    assert (
        cli.main(
            [
                "convert",
                "--source",
                "nope",
                "--input",
                str(source),
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 1
    )
    assert "swebench_lite" in capsys.readouterr().err


def test_cli_bare_input_requires_source(tmp_path, row, capsys):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps([row]))
    assert cli.main(["convert", "--input", str(path), "--output", str(tmp_path / "out")]) == 1
    assert "--source" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_selection_flags(tmp_path, row, capsys):
    second = row | {"instance_id": "example__repo-2"}
    source = tmp_path / "input.json"
    source.write_text(json.dumps([row, second]))
    assert (
        cli.main(
            [
                "convert",
                "--source",
                "swebench_lite",
                "--input",
                str(source),
                "--instance-id",
                second["instance_id"],
                "--instance-id",
                row["instance_id"],
                "--limit",
                "1",
                "--output",
                str(tmp_path / "one"),
            ]
        )
        == 0
    )
    capsys.readouterr()
    manifest = load_dataset(tmp_path / "one")[0]
    assert [t.task_id for t in manifest.tasks] == [second["instance_id"]]


@pytest.mark.parametrize(
    "flags", [["--online"], ["--revision", "main"], ["--split", "dev"], ["--dataset", "lite"]]
)
def test_cli_rejects_removed_conversion_flags(tmp_path, capsys, flags):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "convert",
                "--source",
                "swebench_lite",
                "--input",
                "rows.json",
                "--output",
                str(tmp_path / "out"),
                *flags,
            ]
        )
    assert exit_info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_requires_input(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["convert", "--source", "swebench_lite", "--output", str(tmp_path / "out")])
    assert exit_info.value.code == 2
    assert "--input" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["llm", "ask", "hi"],
        ["tasks", "sources"],
        ["tasks", "convert"],
        ["tasks", "inspect"],
        ["tasks", "prepare-images", "data/tasks/lite"],
    ],
)
def test_cli_rejects_removed_commands(argv):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


@pytest.mark.parametrize("format", ["jsonl", "parquet"])
def test_cli_download_dataset_then_convert(tmp_path, monkeypatch, row, capsys, format):
    if format == "parquet":
        pytest.importorskip("pyarrow")
    from palinreplay.tasks.sources.swebench import common as sb

    def fake_fetch(dataset, split, revision, *, variant):
        return [row], DatasetSource(
            source=f"swebench_{variant}", dataset=dataset, split=split, revision="d" * 40
        )

    monkeypatch.setattr(sb, "fetch", fake_fetch)
    raw = tmp_path / "raw"
    assert (
        cli.main(
            [
                "download",
                "dataset",
                "--source",
                "swebench_lite",
                "--format",
                format,
                "--split",
                "dev",
                "--output",
                str(raw),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Downloaded 1 rows" in out and "revision=" in out
    assert (raw / f"rows.{format}").is_file() and (raw / "fetch.json").is_file()

    def forbidden(*args, **kwargs):
        raise AssertionError("conversion must be offline and must not use Docker")

    monkeypatch.setattr(sb, "fetch", forbidden)
    monkeypatch.setattr(cli, "prepare_images", forbidden)
    assert cli.main(["convert", "--input", str(raw), "--output", str(tmp_path / "tasks")]) == 0
    manifest, tasks = load_dataset(tmp_path / "tasks")
    assert manifest.source == load_fetch(raw).as_dataset_source()
    assert tasks[0].spec.source == manifest.source
    capsys.readouterr()

    # An explicit conflicting adapter must not silently relabel the data.
    assert (
        cli.main(
            [
                "convert",
                "--source",
                "swebench_verified",
                "--input",
                str(raw),
                "--output",
                str(tmp_path / "bad"),
            ]
        )
        == 1
    )
    assert "conflicts with fetch.json" in capsys.readouterr().err
    assert not (tmp_path / "bad").exists()


def test_cli_download_rejects_bad_limit(tmp_path, capsys):
    assert (
        cli.main(
            [
                "download",
                "dataset",
                "--source",
                "swebench_lite",
                "--limit",
                "0",
                "--output",
                str(tmp_path / "raw"),
            ]
        )
        == 1
    )
    assert "limit" in capsys.readouterr().err
    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize("archive", [False, True])
def test_download_mirror_parser(archive):
    argv = ["download", "mirror", "data/tasks/lite"]
    if archive:
        argv += ["--archive-dir", "data/archives"]
    args = cli.build_parser().parse_args(argv)
    assert args.command == "download"
    assert args.download_command == "mirror"
    assert args.dataset_dir == Path("data/tasks/lite")
    assert args.archive_dir == (Path("data/archives") if archive else None)


@pytest.mark.parametrize(
    "argv", [["download", "mirror"], ["tasks", "prepare-images", "data/tasks/lite"]]
)
def test_invalid_mirror_command_does_not_run_any_backend(monkeypatch, capsys, argv):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid arguments must not invoke a backend")

    monkeypatch.setattr(cli, "prepare_images", forbidden)
    monkeypatch.setattr(cli, "download_dataset", forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_download_mirror_failure_is_reported(monkeypatch, tmp_path, capsys):
    def fail_images(dataset_dir, *, archive_dir, show_progress):
        assert dataset_dir == tmp_path
        assert archive_dir is None
        assert show_progress is True
        raise RuntimeError("mock image preparation failure")

    monkeypatch.setattr(cli, "prepare_images", fail_images)
    monkeypatch.setattr(
        cli, "download_dataset", lambda *a, **k: pytest.fail("not dataset download")
    )
    assert cli.main(["download", "mirror", str(tmp_path)]) == 1
    result = capsys.readouterr()
    assert result.err.strip() == "error: mock image preparation failure"
    assert not result.out
    assert not (tmp_path / "images.json").exists()
