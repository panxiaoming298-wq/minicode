"""Multi-variable Decoupling Controller based on Engineering Cybernetics.

钱学森工程控制论核心原理:
- 多变量系统：多个输入输出相互耦合的复杂系统
- 解耦控制：消除变量间相互影响，实现独立控制
- 前馈补偿：预测耦合效应并提前补偿

This module implements:
1. Coupling matrix analysis
2. Decoupling controller design
3. Feedforward compensation for coupling effects
4. Relative gain array (RGA) analysis
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CouplingMatrix:
    """Coupling strength between system variables."""
    token_usage_to_latency: float = 0.0
    context_pressure_to_errors: float = 0.0
    concurrency_to_stability: float = 0.0
    model_level_to_cost: float = 0.0
    skill_complexity_to_timeout: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_matrix(self) -> list[list[float]]:
        return [
            [1.0, self.token_usage_to_latency, self.context_pressure_to_errors],
            [self.token_usage_to_latency, 1.0, self.concurrency_to_stability],
            [self.context_pressure_to_errors, self.concurrency_to_stability, 1.0],
        ]


@dataclass
class DecoupledCommand:
    """Decoupled control command."""
    variable_name: str
    original_command: float
    coupling_compensation: float
    final_command: float
    confidence: float = 1.0
    reasoning: str = ""


class CouplingAnalyzer:
    """Analyze coupling between system variables.

    耦合分析器:
    识别系统中变量间的相互影响关系。
    """

    def __init__(self, window_size: int = 50):
        self._window_size = window_size
        self._history_a: list[float] = []
        self._history_b: list[float] = []
        self._sample_count: int = 0

    def add_sample(self, var_a: float, var_b: float) -> None:
        self._history_a.append(var_a)
        self._history_b.append(var_b)
        self._sample_count += 1

        if len(self._history_a) > self._window_size:
            self._history_a.pop(0)
        if len(self._history_b) > self._window_size:
            self._history_b.pop(0)

    def compute_coupling(self) -> float:
        if len(self._history_a) < 5:
            return 0.0

        correlation = self._pearson_correlation(self._history_a, self._history_b)
        return abs(correlation)

    def compute_time_lagged_coupling(self, lag: int = 1) -> float:
        if len(self._history_a) < lag + 3:
            return 0.0

        var_a_lagged = self._history_a[:-lag] if lag > 0 else self._history_a
        var_b_shifted = self._history_b[lag:] if lag > 0 else self._history_b

        if len(var_a_lagged) < 3 or len(var_b_shifted) < 3:
            return 0.0

        return abs(self._pearson_correlation(var_a_lagged, var_b_shifted))

    def get_coupling_matrix(self) -> CouplingMatrix:
        return CouplingMatrix(
            token_usage_to_latency=self.compute_coupling(),
            context_pressure_to_errors=self.compute_coupling(),
            concurrency_to_stability=self.compute_coupling(),
            model_level_to_cost=self.compute_coupling(),
            skill_complexity_to_timeout=self.compute_coupling(),
        )

    def reset(self) -> None:
        self._history_a = []
        self._history_b = []
        self._sample_count = 0

    def _pearson_correlation(self, x: list[float], y: list[float]) -> float:
        n = min(len(x), len(y))
        if n < 3:
            return 0.0

        x = x[-n:]
        y = y[-n:]

        x_mean = sum(x) / n
        y_mean = sum(y) / n

        numerator = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))

        x_var = sum((xi - x_mean) ** 2 for xi in x)
        y_var = sum((yi - y_mean) ** 2 for yi in y)

        denominator = math.sqrt(x_var * y_var)
        if denominator == 0:
            return 0.0

        return numerator / denominator


class DecouplingController:
    """Multi-variable decoupling controller.

    多变量解耦控制器:
    ┌──────────────────────────────────────────────────────────┐
    │  控制输入 ─→ 解耦网络 ─→ 独立控制通道 ─→ 系统           │
    │    (u1, u2)     (消除耦合)     (独立调节)                │
    │                    ↓                                     │
    │              前馈耦合补偿                                 │
    └──────────────────────────────────────────────────────────┘

    Features:
    - Real-time coupling analysis
    - Decoupling matrix computation
    - Feedforward coupling compensation
    - RGA-based input-output pairing
    """

    def __init__(self):
        self._coupling_analyzers: dict[str, CouplingAnalyzer] = {}
        self._decoupling_matrix: dict[str, dict[str, float]] = {}
        self._command_history: list[DecoupledCommand] = []
        self._max_history = 50

        self._coupling_compensation: dict[str, float] = {}

        self._init_coupling_pairs()

    def _init_coupling_pairs(self) -> None:
        pairs = [
            ("token_usage", "latency"),
            ("context_pressure", "error_rate"),
            ("concurrency", "stability"),
            ("model_level", "cost"),
            ("skill_complexity", "timeout"),
        ]
        for var_a, var_b in pairs:
            key = f"{var_a}_to_{var_b}"
            self._coupling_analyzers[key] = CouplingAnalyzer()
            self._coupling_compensation[key] = 0.0

    def record_measurement(self, variable_pairs: dict[str, tuple[float, float]]) -> None:
        for key, (val_a, val_b) in variable_pairs.items():
            if key in self._coupling_analyzers:
                self._coupling_analyzers[key].add_sample(val_a, val_b)

    def compute_decoupling_matrix(self) -> dict[str, dict[str, float]]:
        self._decoupling_matrix = {}

        for key, analyzer in self._coupling_analyzers.items():
            coupling_strength = analyzer.compute_coupling()

            var_a, var_b = key.split("_to_")
            if var_a not in self._decoupling_matrix:
                self._decoupling_matrix[var_a] = {}
            if var_b not in self._decoupling_matrix:
                self._decoupling_matrix[var_b] = {}

            self._decoupling_matrix[var_a][var_b] = coupling_strength
            self._decoupling_matrix[var_b][var_a] = coupling_strength

        return self._decoupling_matrix

    def decouple_command(self, variable_name: str, raw_command: float,
                         other_variables: dict[str, float]) -> DecoupledCommand:
        coupling_compensation = 0.0

        if variable_name in self._decoupling_matrix:
            for other_var, coupling_strength in self._decoupling_matrix[variable_name].items():
                if other_var in other_variables:
                    coupling_compensation += coupling_strength * other_variables[other_var] * 0.5

        final_command = raw_command - coupling_compensation
        final_command = max(-1.0, min(1.0, final_command))

        command = DecoupledCommand(
            variable_name=variable_name,
            original_command=raw_command,
            coupling_compensation=coupling_compensation,
            final_command=final_command,
            confidence=max(0.3, 1.0 - coupling_compensation),
            reasoning=f"Decoupled {variable_name}: raw={raw_command:.2f}, "
                      f"compensation={coupling_compensation:.2f}, final={final_command:.2f}",
        )

        self._command_history.append(command)
        if len(self._command_history) > self._max_history:
            self._command_history.pop(0)

        return command

    def compute_feedforward_compensation(self, planned_changes: dict[str, float]) -> dict[str, float]:
        compensation = {}

        for var_a, change_a in planned_changes.items():
            total_coupling_effect = 0.0
            if var_a in self._decoupling_matrix:
                for var_b, coupling in self._decoupling_matrix[var_a].items():
                    if var_b in planned_changes:
                        total_coupling_effect += coupling * change_a * planned_changes[var_b]

            compensation[var_a] = total_coupling_effect

        return compensation

    def get_rga_pairing(self) -> list[tuple[str, str, float]]:
        rga_pairs = []

        for key, analyzer in self._coupling_analyzers.items():
            coupling = analyzer.compute_coupling()
            lagged = analyzer.compute_time_lagged_coupling(lag=1)

            if coupling > 0.3 or lagged > 0.3:
                var_a, var_b = key.split("_to_")
                rga_pairs.append((var_a, var_b, max(coupling, lagged)))

        rga_pairs.sort(key=lambda x: x[2], reverse=True)
        return rga_pairs

    def get_coupling_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "coupling_strengths": {},
            "compensation_values": self._coupling_compensation.copy(),
        }

        for key, analyzer in self._coupling_analyzers.items():
            coupling = analyzer.compute_coupling()
            status["coupling_strengths"][key] = coupling

        status["strong_couplings"] = [
            (k, v) for k, v in status["coupling_strengths"].items() if v > 0.5
        ]

        status["rga_pairing"] = self.get_rga_pairing()

        return status

    def reset(self) -> None:
        for analyzer in self._coupling_analyzers.values():
            analyzer.reset()
        self._decoupling_matrix = {}
        self._command_history = []
        self._coupling_compensation = {}
