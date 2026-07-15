from __future__ import annotations

import atexit
import copy
import json
import logging
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.models import CopperLayer, DielectricLayer, Stackup

DEFAULT_SIGMA_S_PER_M = 5.8e7
DEFAULT_SWEEP_START_GHZ = 0.1
DEFAULT_SWEEP_STOP_GHZ = 25.0
DEFAULT_SWEEP_POINTS = 200
DEFAULT_INTERP_TOLERANCE = 0.005
DEFAULT_INITIAL_POINTS = 12
DEFAULT_MAX_SWEEP_POINTS = 200
DEFAULT_MAX_SWEEP_ITERATIONS = 8
DEFAULT_ADAPTIVE_MAX_ITERS = 10
DEFAULT_ADAPTIVE_ENERGY_TOL = 0.01
DEFAULT_ADAPTIVE_PARAM_TOL = 0.1
DEFAULT_ADAPTIVE_MAX_NODES = 20_000
DEFAULT_ADAPTIVE_MIN_PASSES = 2
DEFAULT_INITIAL_GRID = 20
DEFAULT_TARGET_WIDTH_TOLERANCE = 0.001
DEFAULT_WIDTH_SEARCH_REFINE_ITERATIONS = 12
DEFAULT_IMPEDANCE_PLOT_MIN_MIL = 2.0
DEFAULT_IMPEDANCE_PLOT_MAX_MIL = 30.0
DEFAULT_IMPEDANCE_PLOT_SINGLE_POINTS = 10
DEFAULT_IMPEDANCE_PLOT_DIFF_POINTS = 15
DEFAULT_IMPEDANCE_PLOT_GRID = 14
DEFAULT_IMPEDANCE_PLOT_FAST_ADAPTIVE = {
    "max_iters": 30,
    "energy_tol": 0.03,
    "param_tol": 0.25,
    "max_nodes": 12_000,
    "min_converged_passes": 1,
}
DEFAULT_WIDTH_SEARCH_FAST_ADAPTIVE = {
    "max_iters": 1,
    "energy_tol": 0.05,
    "param_tol": 0.5,
    "max_nodes": 12_000,
    "min_converged_passes": 1,
}
DEFAULT_RUNNER_TIMEOUT_SECONDS = 600.0

logger = logging.getLogger(__name__)


class FieldSolverBridgeError(RuntimeError):
    """Raised when the external JS/WASM field solver cannot be executed."""


@dataclass(frozen=True)
class DielectricAggregate:
    total_thickness_mm: float
    effective_dk: float
    average_df: float
    average_freq_ghz: float
    layer_count: int


def _find_adjacent_copper(stackup: Stackup, index: int) -> tuple[int | None, int | None]:
    previous_index = None
    for candidate in range(index - 1, -1, -1):
        if isinstance(stackup.layers[candidate], CopperLayer):
            previous_index = candidate
            break

    next_index = None
    for candidate in range(index + 1, len(stackup.layers)):
        if isinstance(stackup.layers[candidate], CopperLayer):
            next_index = candidate
            break

    return previous_index, next_index


def _dielectric_layers_between(stackup: Stackup, start_index: int | None, end_index: int | None) -> list[tuple[int, DielectricLayer]]:
    if start_index is None or end_index is None:
        return []
    result: list[tuple[int, DielectricLayer]] = []
    for index in range(start_index + 1, end_index):
        layer = stackup.layers[index]
        if isinstance(layer, DielectricLayer):
            result.append((index, layer))
    return result


def aggregate_dielectrics(
    stackup: Stackup,
    catalog: MaterialCatalog,
    dielectric_layers: list[tuple[int, DielectricLayer]],
) -> DielectricAggregate | None:
    if not dielectric_layers:
        return None

    total_thickness = 0.0
    weighted_dk = 0.0
    weighted_df = 0.0
    weighted_freq = 0.0

    for _index, layer in dielectric_layers:
        thickness_mm = stackup.dielectric_thickness_mm(layer, catalog)
        if thickness_mm is None:
            raise FieldSolverBridgeError("A dielectric layer is missing thickness information.")
        freq_ghz = stackup.dielectric_frequency_ghz(layer, catalog)
        dk, df = stackup.dielectric_dk_df(layer, catalog)

        total_thickness += thickness_mm
        weighted_dk += thickness_mm * dk
        weighted_df += thickness_mm * df
        weighted_freq += thickness_mm * freq_ghz

    if total_thickness <= 0:
        return None

    return DielectricAggregate(
        total_thickness_mm=total_thickness,
        effective_dk=weighted_dk / total_thickness,
        average_df=weighted_df / total_thickness,
        average_freq_ghz=weighted_freq / total_thickness,
        layer_count=len(dielectric_layers),
    )


def _weighted_reference_frequency_ghz(*aggregates: DielectricAggregate | None) -> float:
    weighted_freq = 0.0
    total_thickness = 0.0
    for aggregate in aggregates:
        if aggregate is None:
            continue
        weighted_freq += aggregate.average_freq_ghz * aggregate.total_thickness_mm
        total_thickness += aggregate.total_thickness_mm
    if total_thickness <= 0:
        return 1.0
    return weighted_freq / total_thickness


def build_solver_request(
    stackup: Stackup,
    catalog: MaterialCatalog,
    copper_index: int,
    *,
    trace_width_mm: float,
    trace_spacing_mm: float,
    ref_above_index: int | None = None,
    ref_below_index: int | None = None,
) -> dict[str, Any]:
    """Build a solver request dict for the given copper layer.

    *ref_above_index* and *ref_below_index* override the automatically found
    adjacent copper reference planes.  Pass the stackup layer index of the
    desired reference copper layer, or leave as None to use the default
    (nearest adjacent copper).
    """
    selected = stackup.layers[copper_index]
    if not isinstance(selected, CopperLayer):
        raise FieldSolverBridgeError("Selected row is not a copper layer.")

    auto_previous, auto_next = _find_adjacent_copper(stackup, copper_index)
    previous_copper = ref_above_index if ref_above_index is not None else auto_previous
    next_copper = ref_below_index if ref_below_index is not None else auto_next
    top_dielectrics = _dielectric_layers_between(stackup, previous_copper, copper_index)
    bottom_dielectrics = _dielectric_layers_between(stackup, copper_index, next_copper)

    copper_number = stackup.copper_layer_number(copper_index)
    total_copper = stackup.copper_count()
    is_top_outer = copper_number == 1
    is_bottom_outer = copper_number == total_copper
    is_outer = is_top_outer or is_bottom_outer
    is_differential = trace_spacing_mm > 0

    if is_top_outer:
        substrate = aggregate_dielectrics(stackup, catalog, bottom_dielectrics)
        top_side = None
    elif is_bottom_outer:
        substrate = aggregate_dielectrics(stackup, catalog, top_dielectrics)
        top_side = None
    else:
        substrate = aggregate_dielectrics(stackup, catalog, bottom_dielectrics)
        top_side = aggregate_dielectrics(stackup, catalog, top_dielectrics)

    if substrate is None:
        raise FieldSolverBridgeError("Could not determine dielectric thickness next to the selected copper layer.")

    reference_freq_ghz = _weighted_reference_frequency_ghz(substrate, top_side)
    request_type = "microstrip" if is_outer else "stripline"
    if is_differential:
        request_type = f"diff_{request_type}"

    trace_thickness_m = selected.thickness_mm / 1000.0
    trace_width_m = trace_width_mm / 1000.0
    trace_spacing_m = trace_spacing_mm / 1000.0 if is_differential else None
    roughness_m = (selected.roughness_um or 0.0) / 1_000_000.0

    solver_options: dict[str, Any] = {
        "trace_width": trace_width_m,
        "substrate_height": substrate.total_thickness_mm / 1000.0,
        "trace_thickness": trace_thickness_m,
        "epsilon_r": substrate.effective_dk,
        "tan_delta": substrate.average_df,
        "sigma_cond": DEFAULT_SIGMA_S_PER_M,
        "freq": reference_freq_ghz * 1e9,
        "rq": roughness_m,
        "nx": DEFAULT_INITIAL_GRID,
        "ny": DEFAULT_INITIAL_GRID,
    }

    if is_differential and trace_spacing_m is not None:
        solver_options["trace_spacing"] = trace_spacing_m

    if is_outer:
        solver_options["use_sm"] = True
        solver_options["sm_t_sub"] = stackup.soldermask.thickness_mm / 1000.0
        solver_options["sm_t_trace"] = stackup.soldermask.thickness_mm / 1000.0
        solver_options["sm_t_side"] = stackup.soldermask.thickness_mm / 1000.0
        solver_options["sm_er"] = stackup.soldermask.dk
        solver_options["sm_tand"] = stackup.soldermask.df
        solver_options["epsilon_r_top"] = 1.0
        solver_options["tan_delta_top"] = 0.0
        solver_options["boundaries"] = ["open", "open", "open", "gnd"]
    else:
        if top_side is None:
            raise FieldSolverBridgeError("Could not determine the dielectric stack above the selected inner copper layer.")
        solver_options["epsilon_r_top"] = top_side.effective_dk
        solver_options["tan_delta_top"] = top_side.average_df
        solver_options["enclosure_height"] = (top_side.total_thickness_mm + selected.thickness_mm) / 1000.0
        solver_options["boundaries"] = ["open", "open", "gnd", "gnd"]

    return {
        "tl_type": request_type,
        "reference_freq_ghz": reference_freq_ghz,
        "selected_copper": {
            "index": copper_index,
            "label": f"L{copper_number}",
            "copper_type": selected.copper_type,
            "roughness_um": selected.roughness_um,
            "thickness_mm": selected.thickness_mm,
            "is_outer": is_outer,
        },
        "geometry": {
            "trace_width_mm": trace_width_mm,
            "trace_spacing_mm": trace_spacing_mm,
            "substrate": substrate.__dict__,
            "top_side": None if top_side is None else top_side.__dict__,
        },
        "solver_options": solver_options,
        "adaptive_options": {
            "max_iters": DEFAULT_ADAPTIVE_MAX_ITERS,
            "energy_tol": DEFAULT_ADAPTIVE_ENERGY_TOL,
            "param_tol": DEFAULT_ADAPTIVE_PARAM_TOL,
            "max_nodes": DEFAULT_ADAPTIVE_MAX_NODES,
            "min_converged_passes": DEFAULT_ADAPTIVE_MIN_PASSES,
        },
        "sweep": {
            "enabled": True,
            "interpolating": True,
            "start_ghz": DEFAULT_SWEEP_START_GHZ,
            "stop_ghz": DEFAULT_SWEEP_STOP_GHZ,
            "points": DEFAULT_SWEEP_POINTS,
            "tolerance": DEFAULT_INTERP_TOLERANCE,
            "initial_points": DEFAULT_INITIAL_POINTS,
            "max_points": DEFAULT_MAX_SWEEP_POINTS,
            "max_iterations": DEFAULT_MAX_SWEEP_ITERATIONS,
        },
    }


def find_node_executable(root_path: Path) -> Path | None:
    override = os.environ.get("STACKUP_EDITOR_NODE")
    if override:
        override_path = Path(override).expanduser()
        if override_path.exists():
            return override_path
        logger.warning("STACKUP_EDITOR_NODE points to a missing path: %s", override_path)

    bundled_candidates = [
        root_path / "runtime" / "node" / "node.exe",
        root_path / "node" / "node.exe",
    ]
    for candidate in bundled_candidates:
        if candidate.exists():
            return candidate

    which_node = shutil.which("node")
    if which_node:
        return Path(which_node)

    common_paths = [
        Path(r"C:\Program Files\nodejs\node.exe"),
        Path(r"C:\Program Files (x86)\nodejs\node.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "nodejs" / "node.exe",
    ]
    for candidate in common_paths:
        if candidate.exists():
            return candidate

    emsdk_node = root_path / "emsdk-main" / "node"
    if emsdk_node.exists():
        matches = sorted(emsdk_node.glob("**/node.exe"))
        if matches:
            return matches[0]

    codex_cache = Path.home() / ".cache" / "codex-runtimes"
    if codex_cache.exists():
        matches = sorted(codex_cache.glob("**/node.exe"))
        if matches:
            return matches[0]

    return None


def _parse_solver_output(stdout: str, stderr: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        detail = stderr.strip()
        if detail:
            raise FieldSolverBridgeError(f"The solver did not return JSON output. Details:\n{detail}")
        raise FieldSolverBridgeError("The solver did not return JSON output.")

    candidates = [text]
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if lines and lines[-1] != text:
        candidates.append(lines[-1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    snippet = (lines[-1] if lines else text)[:400]
    detail = stderr.strip()
    if detail:
        raise FieldSolverBridgeError(
            "The solver returned invalid JSON output.\n"
            f"Stdout tail: {snippet}\n"
            f"Stderr tail: {detail[:400]}"
        )
    raise FieldSolverBridgeError(f"The solver returned invalid JSON output.\nStdout tail: {snippet}")


def _runner_path(root_path: Path) -> Path:
    runner_path = root_path / "tools" / "field_solver_runner.mjs"
    if not runner_path.exists():
        raise FieldSolverBridgeError(f"Solver runner was not found at {runner_path}.")
    return runner_path


def _request_summary(request: dict[str, Any]) -> str:
    base_request = request.get("base_request")
    if not isinstance(base_request, dict):
        base_request = request

    selected_copper = base_request.get("selected_copper")
    if not isinstance(selected_copper, dict):
        selected_copper = {}
    geometry = base_request.get("geometry")
    if not isinstance(geometry, dict):
        geometry = {}

    mode = str(request.get("mode") or "solve")
    tl_type = str(base_request.get("tl_type") or "<unknown>")
    layer = str(selected_copper.get("label") or selected_copper.get("index") or "<unknown>")
    width_mm = geometry.get("trace_width_mm")
    spacing_mm = geometry.get("trace_spacing_mm")

    extra = ""
    if mode == "impedance_profile_plot":
        width_count = len(request.get("widths_mil") or [])
        gap_count = len(request.get("gaps_mil") or [])
        extra = f" width_samples={width_count} gap_samples={gap_count}"

    return (
        f"mode={mode} tl_type={tl_type} layer={layer} "
        f"width_mm={width_mm!r} spacing_mm={spacing_mm!r}{extra}"
    )


class _PersistentNodeWorker:
    def __init__(self, *, root_path: Path, node_executable: Path, runner_path: Path) -> None:
        self.root_path = root_path
        self.node_executable = node_executable
        self.runner_path = runner_path
        self._process: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[tuple[str, str]] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._start_lock = threading.Lock()
        self._request_lock = threading.Lock()

    def close(self) -> None:
        process = self._process
        self._process = None
        self._stdout_queue = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        finally:
            try:
                if process.stdout:
                    process.stdout.close()
            except OSError:
                pass
            try:
                if process.stderr:
                    process.stderr.close()
            except OSError:
                pass

    def _stderr_detail(self) -> str:
        if not self._stderr_tail:
            return ""
        return "\n".join(self._stderr_tail)

    def _pump_stdout(self, process: subprocess.Popen[str], out_queue: queue.Queue[tuple[str, str]]) -> None:
        stdout = process.stdout
        if stdout is None:
            out_queue.put(("eof", ""))
            return
        try:
            for line in stdout:
                payload = line.rstrip("\r\n")
                if payload:
                    out_queue.put(("line", payload))
        finally:
            out_queue.put(("eof", ""))

    def _pump_stderr(self, process: subprocess.Popen[str]) -> None:
        stderr = process.stderr
        if stderr is None:
            return
        for line in stderr:
            payload = line.rstrip("\r\n")
            if payload:
                self._stderr_tail.append(payload)
                logger.debug("Persistent node stderr: %s", payload)

    def _start(self) -> None:
        with self._start_lock:
            process = self._process
            if process is not None and process.poll() is None:
                return
            self.close()
            self._stderr_tail.clear()
            self._stdout_queue = queue.Queue()
            process = subprocess.Popen(
                [str(self.node_executable), str(self.runner_path), "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.root_path,
                bufsize=1,
            )
            self._process = process
            stdout_thread = threading.Thread(
                target=self._pump_stdout,
                args=(process, self._stdout_queue),
                name="stackup-node-stdout",
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._pump_stderr,
                args=(process,),
                name="stackup-node-stderr",
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

    def request(self, request: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        payload = json.dumps(request, separators=(",", ":"))
        for attempt in range(2):
            self._start()
            with self._request_lock:
                process = self._process
                out_queue = self._stdout_queue
                if process is None or out_queue is None or process.stdin is None:
                    if attempt == 0:
                        self.close()
                        continue
                    raise FieldSolverBridgeError("Persistent Node worker is not available.")

                try:
                    process.stdin.write(payload)
                    process.stdin.write("\n")
                    process.stdin.flush()
                except OSError as exc:
                    self.close()
                    if attempt == 0:
                        continue
                    detail = self._stderr_detail()
                    if detail:
                        raise FieldSolverBridgeError(
                            "Could not write to the persistent Node worker.\n" + detail
                        ) from exc
                    raise FieldSolverBridgeError("Could not write to the persistent Node worker.") from exc

                try:
                    kind, message = out_queue.get(timeout=timeout_seconds)
                except queue.Empty as exc:
                    detail = self._stderr_detail()
                    self.close()
                    if detail:
                        raise FieldSolverBridgeError(
                            "The persistent Node worker timed out.\n" + detail
                        ) from exc
                    raise FieldSolverBridgeError("The persistent Node worker timed out.") from exc

                if kind == "eof":
                    detail = self._stderr_detail()
                    self.close()
                    if attempt == 0:
                        continue
                    if detail:
                        raise FieldSolverBridgeError(
                            "The persistent Node worker exited unexpectedly.\n" + detail
                        )
                    raise FieldSolverBridgeError("The persistent Node worker exited unexpectedly.")

                try:
                    response = json.loads(message)
                except json.JSONDecodeError as exc:
                    detail = self._stderr_detail()
                    self.close()
                    if attempt == 0:
                        continue
                    if detail:
                        raise FieldSolverBridgeError(
                            "The persistent Node worker returned invalid JSON.\n" + detail
                        ) from exc
                    raise FieldSolverBridgeError("The persistent Node worker returned invalid JSON.") from exc

                if not isinstance(response, dict):
                    self.close()
                    if attempt == 0:
                        continue
                    raise FieldSolverBridgeError("The persistent Node worker returned an invalid response.")

                if response.get("ok") is True:
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise FieldSolverBridgeError("The persistent Node worker returned an invalid result payload.")
                    return result

                error_message = str(response.get("error") or "Persistent Node worker request failed.")
                raise FieldSolverBridgeError(error_message)

        raise FieldSolverBridgeError("The persistent Node worker could not complete the request.")


_PERSISTENT_WORKERS: dict[str, _PersistentNodeWorker] = {}
_PERSISTENT_WORKERS_LOCK = threading.Lock()


def _persistent_worker_enabled() -> bool:
    flag = os.environ.get("STACKUP_EDITOR_PERSISTENT_NODE", "1").strip().lower()
    return flag not in {"0", "false", "no"}


def _close_all_persistent_workers() -> None:
    with _PERSISTENT_WORKERS_LOCK:
        workers = list(_PERSISTENT_WORKERS.values())
        _PERSISTENT_WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(_close_all_persistent_workers)


def _get_persistent_worker(root_path: Path, *, node_executable: Path, runner_path: Path) -> _PersistentNodeWorker:
    key = str(root_path.resolve())
    with _PERSISTENT_WORKERS_LOCK:
        worker = _PERSISTENT_WORKERS.get(key)
        if worker is None:
            worker = _PersistentNodeWorker(
                root_path=root_path,
                node_executable=node_executable,
                runner_path=runner_path,
            )
            _PERSISTENT_WORKERS[key] = worker
        return worker


def _run_runner_request_oneshot(root_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    summary = _request_summary(request)
    node_executable = find_node_executable(root_path)
    if node_executable is None:
        logger.error("Node.js could not be resolved for request: %s", summary)
        raise FieldSolverBridgeError(
            "Node.js was not found. Install Node.js or set STACKUP_EDITOR_NODE to a node.exe path."
        )

    runner_path = _runner_path(root_path)
    logger.info("Launching field solver request: %s", summary)
    logger.debug("Using node executable: %s", node_executable)
    logger.debug("Using solver runner: %s", runner_path)
    started_at = time.perf_counter()

    try:
        completed = subprocess.run(
            [str(node_executable), str(runner_path)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            cwd=root_path,
            timeout=DEFAULT_RUNNER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started_at
        logger.error("Field solver timed out after %.2fs: %s", elapsed, summary)
        raise FieldSolverBridgeError("The field solver timed out.") from exc
    except OSError as exc:
        logger.exception("Could not start Node.js for request: %s", summary)
        raise FieldSolverBridgeError(f"Could not start Node.js: {exc}") from exc

    elapsed = time.perf_counter() - started_at
    stdout_tail = completed.stdout.strip()[-400:] if completed.stdout else ""
    stderr_tail = completed.stderr.strip()[-400:] if completed.stderr else ""
    logger.info(
        "Field solver request finished in %.2fs with exit code %s: %s",
        elapsed,
        completed.returncode,
        summary,
    )
    if stderr_tail:
        logger.debug("Field solver stderr tail: %s", stderr_tail)

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        if stdout_tail:
            logger.warning("Field solver stdout tail: %s", stdout_tail)
        if stderr_tail:
            logger.warning("Field solver stderr tail: %s", stderr_tail)
        detail = stderr or stdout or f"Node exited with code {completed.returncode}."
        logger.error("Field solver request failed: %s", detail)
        raise FieldSolverBridgeError(detail)

    try:
        result = _parse_solver_output(completed.stdout, completed.stderr)
    except Exception:
        logger.exception("Failed to parse solver output for request: %s", summary)
        raise

    logger.info("Field solver response parsed successfully: %s", summary)
    return result


def _run_runner_request(root_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    summary = _request_summary(request)
    node_executable = find_node_executable(root_path)
    if node_executable is None:
        logger.error("Node.js could not be resolved for request: %s", summary)
        raise FieldSolverBridgeError(
            "Node.js was not found. Install Node.js or set STACKUP_EDITOR_NODE to a node.exe path."
        )

    runner_path = _runner_path(root_path)
    if not _persistent_worker_enabled():
        return _run_runner_request_oneshot(root_path, request)

    worker = _get_persistent_worker(
        root_path,
        node_executable=node_executable,
        runner_path=runner_path,
    )
    started_at = time.perf_counter()
    try:
        result = worker.request(request, timeout_seconds=DEFAULT_RUNNER_TIMEOUT_SECONDS)
    except FieldSolverBridgeError as exc:
        logger.warning(
            "Persistent node request failed; falling back to one-shot runner: %s | %s",
            summary,
            exc,
        )
        worker.close()
        return _run_runner_request_oneshot(root_path, request)

    elapsed = time.perf_counter() - started_at
    logger.info("Persistent field solver request finished in %.2fs: %s", elapsed, summary)
    return result


def run_solver_request(root_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    return _run_runner_request(root_path, request)


def _build_reference_only_request(
    request: dict[str, Any],
    *,
    adaptive_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared = copy.deepcopy(request)
    prepared["mode"] = "reference_only"
    prepared["sweep"] = {"enabled": False}
    prepared["response_options"] = {
        "include_geometry": False,
        "include_mesh": False,
        "include_visualization": False,
        "include_field_view": False,
    }
    if adaptive_overrides:
        adaptive_options = dict(prepared.get("adaptive_options") or {})
        adaptive_options.update(adaptive_overrides)
        prepared["adaptive_options"] = adaptive_options
    return prepared


def _linspace(start: float, stop: float, count: int) -> list[float]:
    if count <= 1:
        return [float(start)]
    return [
        start + (stop - start) * index / (count - 1)
        for index in range(count)
    ]


def build_impedance_profile_plot_request(
    base_request: dict[str, Any],
    *,
    target_impedance_ohm: float | None = None,
    min_mil: float = DEFAULT_IMPEDANCE_PLOT_MIN_MIL,
    max_mil: float = DEFAULT_IMPEDANCE_PLOT_MAX_MIL,
    single_points: int = DEFAULT_IMPEDANCE_PLOT_SINGLE_POINTS,
    differential_points: int = DEFAULT_IMPEDANCE_PLOT_DIFF_POINTS,
) -> dict[str, Any]:
    plot_request = {
        "mode": "impedance_profile_plot",
        "base_request": copy.deepcopy(base_request),
        "target_freq_ghz": float(base_request.get("reference_freq_ghz") or 0.0),
        "initial_grid": DEFAULT_IMPEDANCE_PLOT_GRID,
        "fast_adaptive_options": dict(DEFAULT_IMPEDANCE_PLOT_FAST_ADAPTIVE),
        "widths_mil": _linspace(min_mil, max_mil, single_points),
    }
    if target_impedance_ohm is not None:
        plot_request["target_impedance_ohm"] = float(target_impedance_ohm)
    if str(base_request.get("tl_type") or "").startswith("diff_"):
        plot_request["widths_mil"] = _linspace(min_mil, max_mil, differential_points)
        plot_request["gaps_mil"] = _linspace(min_mil, max_mil, differential_points)
    return plot_request


def run_impedance_profile_plot_request(root_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    return _run_runner_request(root_path, request)


def find_width_for_impedance(
    root_path: Path,
    stackup: "Stackup",
    catalog: "MaterialCatalog",
    copper_index: int,
    target_z0_ohm: float,
    *,
    tolerance_fraction: float = DEFAULT_TARGET_WIDTH_TOLERANCE,
    width_min_mm: float = 0.05,
    width_max_mm: float = 5.0,
    max_iterations: int = 30,
    ref_above_index: int | None = None,
    ref_below_index: int | None = None,
) -> tuple[float, float]:
    """Solve trace width until Z0 is within *tolerance_fraction* of *target_z0_ohm*.

    Uses a safeguarded Illinois/regula-falsi search to reduce solver calls
    versus midpoint-only bisection while keeping the solution bracketed. The
    fast search stage uses relaxed adaptive settings; the best result is then
    verified and, if needed, refined with exact settings in a smaller local
    bracket.

    Returns (found_width_mm, achieved_z0_ohm).
    Raises FieldSolverBridgeError when the target is outside the reachable range
    or when the solver itself fails.
    """
    if target_z0_ohm <= 0:
        raise FieldSolverBridgeError("Target impedance must be a positive number.")

    cache: dict[tuple[float, bool], float] = {}

    def _relative_error(z0_ohm: float) -> float:
        return abs(z0_ohm - target_z0_ohm) / target_z0_ohm

    def _is_bracketed(z_left: float, z_right: float) -> bool:
        lower = min(z_left, z_right)
        upper = max(z_left, z_right)
        return lower <= target_z0_ohm <= upper

    def _select_best(
        current_best: tuple[float, float],
        candidate: tuple[float, float],
    ) -> tuple[float, float]:
        if _relative_error(candidate[1]) < _relative_error(current_best[1]):
            return candidate
        return current_best

    def _z0_at(width_mm: float, *, fast: bool) -> float:
        key = (round(width_mm, 12), fast)
        cached = cache.get(key)
        if cached is not None:
            return cached

        request = build_solver_request(
            stackup,
            catalog,
            copper_index,
            trace_width_mm=width_mm,
            trace_spacing_mm=0.0,
            ref_above_index=ref_above_index,
            ref_below_index=ref_below_index,
        )
        if fast:
            request = _build_reference_only_request(
                request,
                adaptive_overrides=DEFAULT_WIDTH_SEARCH_FAST_ADAPTIVE,
            )
        else:
            request = _build_reference_only_request(request)
        result = run_solver_request(root_path, request)
        z0_ohm = float(result["solved"]["z0_ohm"])
        cache[key] = z0_ohm
        return z0_ohm

    def _solve_interval(
        lo_width: float,
        hi_width: float,
        *,
        fast: bool,
        max_steps: int,
        initial_lo_z: float,
        initial_hi_z: float,
    ) -> tuple[float, float, float, float]:
        lo = lo_width
        hi = hi_width
        lo_z = initial_lo_z
        hi_z = initial_hi_z
        lo_error = lo_z - target_z0_ohm
        hi_error = hi_z - target_z0_ohm
        best = (lo, lo_z) if _relative_error(lo_z) <= _relative_error(hi_z) else (hi, hi_z)
        weighted_lo_error = lo_error
        weighted_hi_error = hi_error
        retained_side: str | None = None

        for _ in range(max_steps):
            denominator = weighted_hi_error - weighted_lo_error
            if abs(denominator) <= 1e-18:
                next_width = (lo + hi) / 2.0
            else:
                next_width = ((lo * weighted_hi_error) - (hi * weighted_lo_error)) / denominator
                span = hi - lo
                min_margin = max(span * 0.02, 1e-9)
                if (
                    not math.isfinite(next_width)
                    or next_width <= lo
                    or next_width >= hi
                    or (next_width - lo) < min_margin
                    or (hi - next_width) < min_margin
                ):
                    next_width = (lo + hi) / 2.0

            z_next = _z0_at(next_width, fast=fast)
            next_error = z_next - target_z0_ohm
            best = _select_best(best, (next_width, z_next))
            if _relative_error(z_next) <= tolerance_fraction:
                return next_width, z_next, lo, hi

            if lo_error * next_error <= 0:
                hi = next_width
                hi_z = z_next
                hi_error = next_error
                weighted_hi_error = hi_error
                if retained_side == "lo":
                    weighted_lo_error *= 0.5
                else:
                    weighted_lo_error = lo_error
                retained_side = "lo"
            else:
                lo = next_width
                lo_z = z_next
                lo_error = next_error
                weighted_lo_error = lo_error
                if retained_side == "hi":
                    weighted_hi_error *= 0.5
                else:
                    weighted_hi_error = hi_error
                retained_side = "hi"

        return best[0], best[1], lo, hi

    z_at_min = _z0_at(width_min_mm, fast=False)
    z_at_max = _z0_at(width_max_mm, fast=False)

    if not _is_bracketed(z_at_min, z_at_max):
        lower = min(z_at_min, z_at_max)
        upper = max(z_at_min, z_at_max)
        raise FieldSolverBridgeError(
            f"Target {target_z0_ohm:.1f} Ω is outside the reachable range "
            f"[{lower:.1f} – {upper:.1f} Ω] "
            f"for widths {width_min_mm}–{width_max_mm} mm.\n"
            "Try adjusting the search bounds."
        )

    best_width, best_z0, local_lo, local_hi = _solve_interval(
        width_min_mm,
        width_max_mm,
        fast=True,
        max_steps=max_iterations,
        initial_lo_z=z_at_min,
        initial_hi_z=z_at_max,
    )

    verified_z0 = _z0_at(best_width, fast=False)
    verified_best = (best_width, verified_z0)
    if _relative_error(verified_z0) <= tolerance_fraction:
        return verified_best

    local_lo_z = _z0_at(local_lo, fast=False)
    local_hi_z = _z0_at(local_hi, fast=False)
    if _is_bracketed(local_lo_z, local_hi_z):
        refined_width, refined_z0, _unused_lo, _unused_hi = _solve_interval(
            local_lo,
            local_hi,
            fast=False,
            max_steps=min(DEFAULT_WIDTH_SEARCH_REFINE_ITERATIONS, max_iterations),
            initial_lo_z=local_lo_z,
            initial_hi_z=local_hi_z,
        )
        refined_best = (refined_width, refined_z0)
        if _relative_error(refined_best[1]) <= _relative_error(verified_best[1]):
            return refined_best
        return verified_best

    refined_width, refined_z0, _unused_lo, _unused_hi = _solve_interval(
        width_min_mm,
        width_max_mm,
        fast=False,
        max_steps=max_iterations,
        initial_lo_z=z_at_min,
        initial_hi_z=z_at_max,
    )
    refined_best = (refined_width, refined_z0)
    if _relative_error(refined_best[1]) <= _relative_error(verified_best[1]):
        return refined_best
    return verified_best


def format_solver_summary(result: dict[str, Any]) -> str:
    reference = result["reference"]
    model = result["tl_type"].replace("_", " ")
    lines = [
        f"{result['selected_copper']['label']} | {model} | ref {reference['freq_ghz']:.3f} GHz",
    ]

    solved = result["solved"]
    if result["is_differential"]:
        lines.extend(
            [
                f"Zdiff {solved['z_diff_ohm']:.2f} ohm | Zcommon {solved['z_common_ohm']:.2f} ohm",
                f"Zodd {solved['z_odd_ohm']:.2f} ohm | Zeven {solved['z_even_ohm']:.2f} ohm",
                f"Eps odd {solved['eps_eff_odd']:.3f} | Eps even {solved['eps_eff_even']:.3f}",
            ]
        )
    else:
        lines.extend(
            [
                f"Z0 {solved['z0_ohm']:.2f} ohm | Eps_eff {solved['eps_eff']:.3f}",
            ]
        )

    lines.append(
        f"Loss @ ref: conductor {solved['alpha_c_db_per_m']:.3f} dB/m | "
        f"dielectric {solved['alpha_d_db_per_m']:.3f} dB/m | total {solved['alpha_total_db_per_m']:.3f} dB/m"
    )

    sweep = result.get("sweep")
    if sweep:
        start = sweep["start"]
        stop = sweep["stop"]
        lines.append(
            f"Loss sweep: {start['alpha_total_db_per_m']:.3f} dB/m @ {start['freq_ghz']:.2f} GHz -> "
            f"{stop['alpha_total_db_per_m']:.3f} dB/m @ {stop['freq_ghz']:.2f} GHz"
        )

    return "\n".join(lines)
