"""Build a frozen C0 with ``palin preload SOURCE [--output NEW_DIRECTORY]``.

SOURCE is a prepared task package (use --task-id for multiple tasks), or standalone
preload-input JSON. The default preserves the image: initialization checks the
configured shell, workdir and BASH_ENV without executing instruction as a script.
For additional setup, --init reads InitializationSpec JSON, for example::

    {"commands": ["python prepare.py"], "checks": ["test -f ready"]}

Each command uses a fresh configured shell. Checks must be nonempty and succeed
again before freezing. Only record's safe environment keys and explicit
--public-env names are saved in launch metadata; host variables are not copied.
The resulting base can be selected with record run --preload.
"""

import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from palinreplay.cli.common import cancellation_signals
from palinreplay.cli.outputs import default_output
from palinreplay.core.errors import RecordUsageError
from palinreplay.preload.batch import build_preloads, select_preload_inputs
from palinreplay.preload.build import build_preload
from palinreplay.preload.environment import PreloadCleanupError, public_keys
from palinreplay.preload.initialization import InitializationSpec, PreloadInitializationError
from palinreplay.preload.input import _unique_object


def add_commands(commands):
    preload = commands.add_parser(
        "preload", help="initialize an image and freeze a read-only Btrfs C0 base",
        description="Build a C0 base from a prepared task package or preload-input JSON.",
        epilog=(
            "Without --init, preserve the image and check shell/workdir/BASH_ENV. "
            "instruction remains task text. Btrfs requires Linux mount permissions and local "
            "palinreplay/btrfs-tools:0.1 and palinreplay/btrfs-bootstrap:0.1 images. "
        ),
    )
    preload.add_argument("source", type=Path, help="task package directory or preload-input JSON")
    preload.add_argument(
        "--output", type=Path, help="new C0 directory (default: outputs/preload/DATE_TASK)"
    )
    preload.add_argument(
        "--task-id", action="append", default=[], help="select a task; may be repeated",
    )
    preload.add_argument(
        "--init", type=Path, dest="initialization",
        help="JSON with commands and nonempty checks (optional timeout_sec)",
    )
    preload.add_argument(
        "--public-env", action="append", default=[], metavar="KEY",
        help="also save this container environment key in launch metadata; repeatable",
    )


def preload_output_paths(source, inputs, output=None):
    """Plan a fresh single-preload directory or per-task batch directories."""
    source = Path(source)
    if output is None:
        name = (inputs[0].task_id or source.stem) if len(inputs) == 1 else source.name
        output = default_output("preload", name)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError()
    if len(inputs) == 1:
        return output, [output]
    if any(item.task_id == "preloads.json" for item in inputs):
        raise RecordUsageError("task ID conflicts with the batch index name")
    return output, [output / item.task_id for item in inputs]


def execute(args) -> int:
    started = time.monotonic()
    phase = "input"
    try:
        with cancellation_signals():
            if any(not key or "=" in key or "\x00" in key for key in args.public_env):
                raise RecordUsageError("--public-env requires environment key names, without values")
            selected = select_preload_inputs(args.source, args.task_id)
            args.output, paths = preload_output_paths(args.source, selected, args.output)
            if args.initialization is None:
                initialization = InitializationSpec(checks=("pwd -P",))
            else:
                data = json.loads(
                    args.initialization.read_bytes().decode("utf-8"), object_pairs_hook=_unique_object,
                )
                initialization = InitializationSpec.model_validate(data)
            phase = "build"
            if len(selected) == 1:
                # Close image inspection before the single build's owned lifecycle.
                keys = public_keys(selected[0], args.public_env)
                result = build_preload(
                    selected[0], paths[0], initialization=initialization, public_env_keys=keys,
                )
            else:
                build_preloads(
                    args.source, selected, args.output, paths,
                    initialization=initialization, public_env=args.public_env,
                )
        if len(selected) == 1:
            print(f"Preload: {result.preload_id}")
            print(f"Path: {result.path}")
            print(f"Time: {time.monotonic() - started:.3f}s")
        else:
            print(f"Batch: {args.output.absolute()}")
        return 0
    except KeyboardInterrupt:
        print("error: preload cancelled", file=sys.stderr)
        return 130
    except RecordUsageError as error:
        print(f"error: {error}", file=sys.stderr)
    except FileExistsError:
        print("error: output already exists; choose a new output path", file=sys.stderr)
    except ValidationError:
        print("error: invalid preload input or initialization configuration", file=sys.stderr)
    except PreloadInitializationError as error:
        print(
            f"error: preload initialization {error.phase} failed (exit {error.result.exit_code})",
            file=sys.stderr,
        )
    except PreloadCleanupError as error:
        # Names/IDs are generated by our lifecycle, not copied from command output.
        print(f"error: preload container removal unconfirmed: {error.container_name}", file=sys.stderr)
    except Exception as error:
        # SDK errors and JSON validation may include credentials or instruction.
        if phase == "input":
            message = "check source, --task-id and --init JSON"
        else:
            message = "check Docker, the image, initialization checks and the Btrfs tools and bootstrap images"
        print(f"error: preload {phase} failed ({type(error).__name__}); {message}", file=sys.stderr)
    return 1
