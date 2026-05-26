from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParamTarget:
    node_id: str
    field_name: str
    index: int | None = None


@dataclass(frozen=True)
class ParamSpec:
    targets: tuple[ParamTarget, ...]
    type: str
    multi: bool


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    workflow_name: str
    api_workflow_path: str | None
    params: dict[str, ParamSpec]


def _cast(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    if type_name == "str":
        return str(value)
    if type_name == "int":
        if isinstance(value, int):
            return value
        return int(str(value).strip())
    if type_name == "float":
        if isinstance(value, float):
            return value
        return float(str(value).strip())
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        return v in ("1", "true", "yes", "y", "on")
    return value


class WorkflowRegistry:
    def __init__(self, specs: dict[str, WorkflowSpec]) -> None:
        self._specs = specs

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "WorkflowRegistry":
        workflows = config.get("workflows") or {}
        specs: dict[str, WorkflowSpec] = {}
        for key, raw in workflows.items():
            wf_name = (raw or {}).get("workflowName") or key
            api_path = (raw or {}).get("apiWorkflowPath")
            api_workflow_path = str(api_path).strip() if isinstance(api_path, str) and str(api_path).strip() else None
            params: dict[str, ParamSpec] = {}
            for pkey, ps in ((raw or {}).get("params") or {}).items():
                if not isinstance(ps, dict):
                    continue
                targets: list[ParamTarget] = []
                raw_targets = ps.get("targets")
                if isinstance(raw_targets, list):
                    for t in raw_targets:
                        if not isinstance(t, dict):
                            continue
                        node_id = t.get("nodeId")
                        field_name = t.get("fieldName")
                        if node_id is None or field_name is None:
                            continue
                        idx = t.get("index")
                        idx_val = int(idx) if isinstance(idx, int) else (int(str(idx)) if isinstance(idx, str) and str(idx).strip().isdigit() else None)
                        targets.append(ParamTarget(node_id=str(node_id), field_name=str(field_name), index=idx_val))
                else:
                    node_id = ps.get("nodeId")
                    field_name = ps.get("fieldName")
                    if node_id is not None and field_name is not None:
                        targets.append(ParamTarget(node_id=str(node_id), field_name=str(field_name)))

                if not targets:
                    continue
                params[pkey] = ParamSpec(
                    targets=tuple(targets),
                    type=str(ps.get("type") or "str"),
                    multi=bool(ps.get("multi", False)),
                )
            specs[key] = WorkflowSpec(key=key, workflow_name=wf_name, api_workflow_path=api_workflow_path, params=params)
        return cls(specs)

    def get(self, key: str) -> WorkflowSpec | None:
        return self._specs.get(key)

    def first(self) -> WorkflowSpec | None:
        if self._specs:
            return next(iter(self._specs.values()))
        return None

    def build_node_info_list(self, wf: WorkflowSpec, values: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for k, v in values.items():
            spec = wf.params.get(k)
            if not spec:
                continue
            for t in spec.targets:
                vv = v
                if isinstance(vv, list) and t.index is not None:
                    vv = vv[t.index] if 0 <= t.index < len(vv) else None
                if isinstance(vv, list) and not spec.multi and t.index is None:
                    vv = vv[0] if vv else None
                casted = _cast(vv, spec.type)
                if casted is None:
                    continue
                out.append({"nodeId": t.node_id, "fieldName": t.field_name, "fieldValue": casted})
        return out
