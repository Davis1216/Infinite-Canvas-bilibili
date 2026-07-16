import re
import time
from typing import Any, Dict, Iterable, List


KNOWLEDGE_MODES = {"none", "augment", "strict", "maintain"}
ORDINARY_KNOWLEDGE_MODES = {"none", "augment", "strict"}


def _clean_ids(values: Iterable[Any], limit: int = 100) -> List[str]:
    result: List[str] = []
    for value in values or []:
        clean = re.sub(r"[^a-zA-Z0-9_-]", "", str(value or ""))
        if clean and clean not in result:
            result.append(clean)
        if len(result) >= limit:
            break
    return result


def normalize_jinni_knowledge_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the canonical per-capability Jinni knowledge configuration.

    Older Jinni records used one shared knowledge-base pool.  They are expanded
    into the new scopes on read so existing files and frozen snapshots continue
    to work without an eager migration.
    """
    raw = data.get("knowledge_config") if isinstance(data.get("knowledge_config"), dict) else None
    legacy_capabilities = data.get("knowledge_capabilities") if isinstance(data.get("knowledge_capabilities"), dict) else {}
    legacy_ids = _clean_ids(data.get("knowledge_base_ids") or [])
    legacy_strict_id = _clean_ids([data.get("strict_knowledge_base_id") or ""])
    legacy_mode = str(data.get("default_knowledge_mode") or "none").strip().lower()

    if raw is None:
        strict_id = legacy_strict_id[0] if legacy_strict_id else ""
        config = {
            "default_mode": legacy_mode if legacy_mode in KNOWLEDGE_MODES else "none",
            "augment": {"enabled": bool(legacy_capabilities.get("augment")), "knowledge_base_ids": legacy_ids},
            "strict": {"enabled": bool(legacy_capabilities.get("strict")), "knowledge_base_id": strict_id},
            "maintain": {"enabled": bool(legacy_capabilities.get("maintain")), "knowledge_base_ids": legacy_ids},
        }
    else:
        augment = raw.get("augment") if isinstance(raw.get("augment"), dict) else {}
        strict = raw.get("strict") if isinstance(raw.get("strict"), dict) else {}
        maintain = raw.get("maintain") if isinstance(raw.get("maintain"), dict) else {}
        strict_ids = _clean_ids([strict.get("knowledge_base_id") or ""])
        mode = str(raw.get("default_mode") or "none").strip().lower()
        config = {
            "default_mode": mode if mode in KNOWLEDGE_MODES else "none",
            "augment": {"enabled": bool(augment.get("enabled")), "knowledge_base_ids": _clean_ids(augment.get("knowledge_base_ids") or [])},
            "strict": {"enabled": bool(strict.get("enabled")), "knowledge_base_id": strict_ids[0] if strict_ids else ""},
            "maintain": {"enabled": bool(maintain.get("enabled")), "knowledge_base_ids": _clean_ids(maintain.get("knowledge_base_ids") or [])},
        }
    return config


def canonicalize_jinni_knowledge(data: Dict[str, Any]) -> Dict[str, Any]:
    config = normalize_jinni_knowledge_config(data)
    union_ids = _clean_ids([
        *(config["augment"]["knowledge_base_ids"] or []),
        config["strict"]["knowledge_base_id"],
        *(config["maintain"]["knowledge_base_ids"] or []),
    ])
    data["knowledge_config"] = config
    # Keep compatibility fields in saved records while new code uses the
    # canonical structure above.
    data["knowledge_capabilities"] = {
        key: bool(config[key]["enabled"]) for key in ("augment", "strict", "maintain")
    }
    data["knowledge_base_ids"] = union_ids
    data["strict_knowledge_base_id"] = config["strict"]["knowledge_base_id"]
    data["default_knowledge_mode"] = config["default_mode"]
    return data


def validate_jinni_knowledge_config(repository, user_id: str, data: Dict[str, Any], allow_missing: bool = False) -> Dict[str, Any]:
    config = normalize_jinni_knowledge_config(data)
    for mode in ("augment", "maintain"):
        valid = []
        for knowledge_base_id in config[mode]["knowledge_base_ids"]:
            if repository.get_knowledge_base(user_id, knowledge_base_id):
                valid.append(knowledge_base_id)
            elif not allow_missing:
                raise ValueError("选择的知识库不存在或不属于当前用户")
        config[mode]["knowledge_base_ids"] = valid
        if config[mode]["enabled"] and not valid:
            if allow_missing:
                config[mode]["enabled"] = False
            else:
                raise ValueError(f"启用{'结合回答' if mode == 'augment' else '知识维护'}时至少选择一个知识库")

    strict_id = config["strict"]["knowledge_base_id"]
    if strict_id and not repository.get_knowledge_base(user_id, strict_id):
        if allow_missing:
            strict_id = ""
            config["strict"]["knowledge_base_id"] = ""
        else:
            raise ValueError("严格回答知识库不存在或不属于当前用户")
    if config["strict"]["enabled"] and not strict_id:
        if allow_missing:
            config["strict"]["enabled"] = False
        else:
            raise ValueError("启用仅知识库回答时必须选择一个知识库")

    default_mode = config["default_mode"]
    if default_mode != "none" and not config[default_mode]["enabled"]:
        if allow_missing:
            config["default_mode"] = "none"
        else:
            raise ValueError("默认知识模式尚未启用")
    data["knowledge_config"] = config
    return canonicalize_jinni_knowledge(data)


def knowledge_scope_ids(config: Dict[str, Any], mode: str) -> List[str]:
    config = normalize_jinni_knowledge_config({"knowledge_config": config})
    if mode == "strict":
        value = config["strict"]["knowledge_base_id"]
        return [value] if value else []
    if mode in {"augment", "maintain"}:
        return list(config[mode]["knowledge_base_ids"])
    return []


def normalize_ordinary_knowledge_context(repository, user_id: str, raw: Any, existing: Any = None,
                                         refresh: bool = False) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    previous = existing if isinstance(existing, dict) else {}
    mode = str(source.get("mode", previous.get("mode", "none")) or "none").strip().lower()
    if mode not in ORDINARY_KNOWLEDGE_MODES:
        raise ValueError("普通聊天仅支持结合知识库或仅依据知识库回答")
    ids = _clean_ids(source.get("knowledge_base_ids", previous.get("knowledge_base_ids", [])) or [])
    if mode == "none":
        ids = []
    elif not ids:
        raise ValueError("请至少选择一个知识库")
    bases = {}
    for knowledge_base_id in ids:
        item = repository.get_knowledge_base(user_id, knowledge_base_id)
        if not item:
            raise ValueError("选择的知识库不存在或不属于当前用户")
        bases[knowledge_base_id] = item
    old_generations = previous.get("generations") if isinstance(previous.get("generations"), dict) else {}
    generations = {
        knowledge_base_id: int(old_generations.get(knowledge_base_id) or bases[knowledge_base_id].get("active_generation") or 1)
        if not refresh else int(bases[knowledge_base_id].get("active_generation") or 1)
        for knowledge_base_id in ids
    }
    return {
        "mode": mode,
        "knowledge_base_ids": ids,
        "generations": generations,
        "updated_at": int(time.time() * 1000),
    }


def ordinary_context_updates(repository, user_id: str, context: Any) -> List[str]:
    context = context if isinstance(context, dict) else {}
    generations = context.get("generations") if isinstance(context.get("generations"), dict) else {}
    updates = []
    for knowledge_base_id in context.get("knowledge_base_ids") or []:
        item = repository.get_knowledge_base(user_id, knowledge_base_id)
        if item and int(item.get("active_generation") or 1) > int(generations.get(knowledge_base_id) or 0):
            updates.append(knowledge_base_id)
    return updates
