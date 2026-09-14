"""Config read/write service backing the management API.

Responsibilities, in the order they matter:

1. **Read the authoritative config.** Every read goes through
   :func:`aemeath.config.resolve_config_path`, the same function the launcher and
   the runtime use, so there is exactly one answer to "which config is this".
2. **Validate a complete candidate before writing.** A save is rejected as a
   whole; the file on disk is never partially updated and never left
   half-valid. Validation runs the real upstream schema, not a local subset,
   because the server refuses to boot on a schema error and a save that breaks
   startup would be worse than no save at all.
3. **Write atomically.** The candidate is written to a temporary file in the
   same directory and moved into place with ``os.replace``. A failure at any
   point leaves the previous file byte-identical.
4. **Track revisions.** Saving bumps a revision; the process records the
   revision it started with. Comparing the two is what lets the UI distinguish
   "saved" from "in effect" (see ``context.md``: 已保存／已生效).

Credentials: the YAML stores a ``${VAR}`` reference or nothing. A submitted key
is written to the local credential store and referenced by name, so no literal
secret reaches the config file, a response, or a patch.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from loguru import logger

from .. import persona as persona_module
from ..config import CONFIG_PATH_ENV, resolve_config_path
from .schema import (
    FieldError,
    ModelOverview,
    OverviewResponse,
    PersonaOverview,
    SaveResult,
)

#: Environment variable naming the credential store used by the management API.
#: The store is a local key/value file, deliberately not the YAML.
CREDENTIAL_STORE_ENV = "AEMEATH_CREDENTIAL_STORE"

#: Default credential store location, relative to the project root.
DEFAULT_CREDENTIAL_STORE = Path("config") / "credentials.yaml"

#: API key environment-variable name used when the user supplies a key through
#: the management UI. Keeping a fixed name means the upstream ``${VAR}``
#: expansion resolves it the same way it resolves a hand-written reference.
MANAGED_API_KEY_ENV = "AEMEATH_LLM_API_KEY"


def _revision_of(text: str) -> str:
    """A content revision token for a config document.

    A hash of the file text rather than a counter: the revision has to change
    when the content changes and stay equal when it does not, and it must
    survive a restart, since the process needs to compare the revision it
    started with against what is on disk now.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def credential_store_path() -> Path:
    """Where local credentials are kept.

    Overridable through the environment so tests never touch the real store.
    """
    override = os.getenv(CREDENTIAL_STORE_ENV)
    if override:
        return Path(override).expanduser()
    from ..config import ROOT_DIR

    return ROOT_DIR / DEFAULT_CREDENTIAL_STORE


def load_credential_store() -> Dict[str, str]:
    """Read the local credential store, tolerating its absence."""
    path = credential_store_path()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - depends on a damaged store
        logger.error("Credential store is unreadable; treating it as empty: {}", exc)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_credential(name: str, value: str) -> None:
    """Store one credential locally.

    Written with owner-only permissions where the platform supports it. The file
    is excluded from version control (``config/credentials.yaml`` is covered by
    the ``config/local.*`` and ``.env*`` rules — see ``.gitignore``); it is also
    never returned by any response.
    """
    path = credential_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = load_credential_store()
    store[name] = value
    text = yaml.safe_dump(store, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass


def credential_is_configured(raw_reference: Any) -> bool:
    """Whether a config value names a usable credential.

    Covers both accepted forms: a ``${VAR}`` reference (configured if the
    variable or the store has a value) and a literal value that was already in
    the file. The literal is reported as configured but never returned.
    """
    if not isinstance(raw_reference, str) or not raw_reference.strip():
        return False
    value = raw_reference.strip()
    if value.startswith("${") and value.endswith("}"):
        name = value[2:-1]
        if not name:
            return False
        return bool(os.getenv(name)) or name in load_credential_store()
    return True


class ConfigService:
    """Reads and writes the authoritative Aemeath configuration."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        """Bind the service to a config path.

        Args:
            config_path: Explicit path. When omitted the authoritative path is
                resolved through the environment, which is what production does.
                An explicit path is for tooling and tests.
        """
        self._explicit_path = config_path
        #: The revision this process started with. Captured once, at
        #: construction: it is the definition of "what is running", and it must
        #: not advance when the user saves.
        self._startup_revision = self._current_revision()

    # ------------------------------------------------------------------
    # Paths and raw access
    # ------------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        """The authoritative config file."""
        return resolve_config_path(self._explicit_path)

    def read_text(self) -> str:
        """Raw config text, or an empty string when the file is absent."""
        path = self.config_path
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def read_document(self) -> Dict[str, Any]:
        """The parsed config document.

        Parsing uses ``yaml.safe_load`` rather than upstream's ``read_yaml``
        because the latter expands ``${VAR}`` references, and the management
        layer must never hold resolved secrets — it would then be one logging
        mistake away from writing one out.
        """
        text = self.read_text()
        if not text:
            return {}
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            logger.error("Config is not valid YAML: {}", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _current_revision(self) -> str:
        """Revision of the file as it exists right now."""
        return _revision_of(self.read_text())

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        """Everything the overview page needs, as plain data."""
        document = self.read_document()
        saved = self._current_revision()

        model = self._model_overview(document)
        persona_view = persona_module.describe(document)

        response = OverviewResponse(
            config_path=str(self.config_path),
            saved_revision=saved,
            running_revision=self._startup_revision,
            restart_required=saved != self._startup_revision,
            model=model,
            persona=PersonaOverview(**persona_view),
            read_error="" if document else "配置文件为空或无法解析。",
        )
        return response.model_dump()

    def _model_overview(self, document: Dict[str, Any]) -> ModelOverview:
        """The conversation connection, read from the upstream selection.

        Follows the same indirection ``aemeath/config.py`` uses: the active agent
        names a provider, and ``llm_configs`` holds the connection. Reading the
        real selection means the overview cannot drift from what conversations
        actually use.
        """
        character = document.get("character_config") or {}
        agent_config = character.get("agent_config") or {}
        settings = agent_config.get("agent_settings") or {}
        chosen = agent_config.get("conversation_agent_choice")
        pool = agent_config.get("llm_configs") or {}

        block = settings.get(chosen) if chosen else None
        if not isinstance(block, dict):
            block = settings.get("aemeath_agent")
        if not isinstance(block, dict):
            block = next(
                (b for b in settings.values() if isinstance(b, dict) and b.get("llm_provider")),
                None,
            )
        if not isinstance(block, dict):
            return ModelOverview()

        provider_name = block.get("llm_provider") or ""
        provider_block = pool.get(provider_name) if provider_name else None
        if not isinstance(provider_block, dict):
            return ModelOverview(provider=str(provider_name or ""))

        raw_key = provider_block.get("llm_api_key") or ""
        api_key_env = ""
        if isinstance(raw_key, str) and raw_key.startswith("${") and raw_key.endswith("}"):
            api_key_env = raw_key[2:-1]

        return ModelOverview(
            provider=str(provider_name),
            base_url=str(provider_block.get("base_url") or ""),
            model=str(provider_block.get("model") or ""),
            api_key_configured=credential_is_configured(raw_key),
            api_key_env=api_key_env,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_url(raw: str) -> List[FieldError]:
        """Check the endpoint is an absolute http(s) URL."""
        value = (raw or "").strip()
        if not value:
            return [FieldError(field="base_url", message="服务地址不能为空。")]
        if not value.startswith(("http://", "https://")):
            return [
                FieldError(
                    field="base_url",
                    message="服务地址必须以 http:// 或 https:// 开头。",
                )
            ]
        return []

    @staticmethod
    def _validate_model(raw: str) -> List[FieldError]:
        """Check a model name is present."""
        if not (raw or "").strip():
            return [FieldError(field="model", message="模型名称不能为空。")]
        return []

    def _validate_document(self, document: Dict[str, Any]) -> List[FieldError]:
        """Validate a candidate through the real upstream schema.

        The server validates the deployed document at startup and refuses to
        boot when it fails. Running the same validator here means a save can
        never leave a config that will not start — which is the failure the
        "illegal fields must not damage the old file" criterion is about.
        """
        import tempfile as _tempfile

        from src.open_llm_vtuber.config_manager import read_yaml, validate_config

        handle, name = _tempfile.mkstemp(suffix=".yaml")
        os.close(handle)
        candidate = Path(name)
        try:
            candidate.write_text(
                yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
            )
            try:
                validate_config(read_yaml(str(candidate)))
            except Exception as exc:
                return [
                    FieldError(
                        field="config",
                        message=f"配置未通过上游校验：{exc}",
                    )
                ]
            return []
        finally:
            try:
                candidate.unlink()
            except OSError:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _atomic_write(self, document: Dict[str, Any]) -> Tuple[bool, str]:
        """Write the document atomically.

        Returns:
            ``(ok, message)``. On failure the destination is untouched: the
            temporary file is written and only then moved into place, so a
            failure while writing cannot truncate the existing config.
        """
        path = self.config_path
        text = yaml.safe_dump(document, allow_unicode=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
            except BaseException:
                # Remove the temporary sibling; a leftover ``.tmp`` next to the
                # config is clutter the user did not ask for.
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.error("Config write failed; previous config is intact: {}", exc)
            return False, f"保存失败，已保留原配置：{exc}"
        return True, ""

    def _save_result(
        self,
        *,
        ok: bool,
        errors: Optional[List[FieldError]] = None,
        conflict: bool = False,
        message: str = "",
    ) -> Dict[str, Any]:
        """Build a save response, always including both revisions."""
        saved = self._current_revision()
        return SaveResult(
            ok=ok,
            errors=errors or [],
            conflict=conflict,
            message=message,
            saved_revision=saved,
            running_revision=self._startup_revision,
            restart_required=ok and saved != self._startup_revision,
        ).model_dump()

    # ------------------------------------------------------------------
    # Save operations
    # ------------------------------------------------------------------

    def save_model(
        self,
        *,
        base_url: str,
        model: str,
        expected_revision: str,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save the conversation model connection.

        Args:
            base_url: Endpoint base URL.
            model: Model name.
            expected_revision: Revision the client read.
            api_key: Optional credential. Stored locally and referenced by name;
                never written to the YAML and never echoed.

        Returns:
            A :class:`SaveResult` payload.
        """
        errors = self._validate_url(base_url) + self._validate_model(model)
        if errors:
            return self._save_result(ok=False, errors=errors)

        document = self.read_document()
        if not document:
            return self._save_result(
                ok=False,
                errors=[
                    FieldError(
                        field="config", message="现有配置无法解析，拒绝覆盖以免损坏。"
                    )
                ],
            )

        # Revision check before any mutation, so a conflict cannot write.
        current = self._current_revision()
        if expected_revision and expected_revision != current:
            logger.warning(
                "Rejecting a save based on a stale revision ({} != {}).",
                expected_revision,
                current,
            )
            return self._save_result(
                ok=False,
                conflict=True,
                message="配置已被其他操作修改，请刷新后重试。",
            )

        candidate = self._with_model(document, base_url, model, api_key)
        if candidate is None:
            return self._save_result(
                ok=False,
                errors=[
                    FieldError(
                        field="provider",
                        message="配置中找不到对话模型连接（llm_configs）。",
                    )
                ],
            )

        errors = self._validate_document(candidate)
        if errors:
            return self._save_result(ok=False, errors=errors)

        if candidate == document:
            # Nothing changed: report success without claiming a restart.
            return self._save_result(ok=True, message="配置未变化。")

        # Persist the credential *after* validation, so a rejected save leaves
        # no trace of the submitted key anywhere.
        if api_key:
            save_credential(MANAGED_API_KEY_ENV, api_key)

        ok, message = self._atomic_write(candidate)
        if not ok:
            return self._save_result(ok=False, message=message)

        logger.info("Conversation model updated through the management API.")
        return self._save_result(
            ok=True, message="已保存，重启后生效。"
        )

    def _with_model(
        self,
        document: Dict[str, Any],
        base_url: str,
        model: str,
        api_key: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Return a copy of the document with the model connection changed.

        The change is applied to the provider block the active agent actually
        selects, so the UI cannot edit a connection nothing uses.
        """
        import copy

        candidate = copy.deepcopy(document)
        character = candidate.get("character_config")
        if not isinstance(character, dict):
            return None
        agent_config = character.get("agent_config")
        if not isinstance(agent_config, dict):
            return None
        settings = agent_config.get("agent_settings") or {}
        pool = agent_config.get("llm_configs")
        if not isinstance(pool, dict):
            return None

        chosen = agent_config.get("conversation_agent_choice")
        block = settings.get(chosen) if chosen else None
        if not isinstance(block, dict):
            block = settings.get("aemeath_agent")
        if not isinstance(block, dict):
            return None
        provider_name = block.get("llm_provider")
        if not provider_name or not isinstance(pool.get(provider_name), dict):
            return None

        provider_block = pool[provider_name]
        provider_block["base_url"] = base_url.strip()
        provider_block["model"] = model.strip()
        if api_key:
            # A reference, never the value. Upstream expands ``${VAR}`` from the
            # environment, where the runtime looks it up.
            provider_block["llm_api_key"] = "${%s}" % MANAGED_API_KEY_ENV
        return candidate

    def save_persona(
        self,
        *,
        identity: str,
        personality: str,
        reply_style: str,
        expected_revision: str,
    ) -> Dict[str, Any]:
        """Save the three persona fields, switching off the legacy prompt.

        The legacy ``persona_prompt`` is left in the file untouched. Only the
        mode changes, so the original wording stays recoverable.
        """
        errors = persona_module.validate_fields(identity, personality, reply_style)
        if errors:
            return self._save_result(
                ok=False,
                errors=[FieldError(**error) for error in errors],
            )

        document = self.read_document()
        if not document:
            return self._save_result(
                ok=False,
                errors=[
                    FieldError(
                        field="config", message="现有配置无法解析，拒绝覆盖以免损坏。"
                    )
                ],
            )

        current = self._current_revision()
        if expected_revision and expected_revision != current:
            return self._save_result(
                ok=False,
                conflict=True,
                message="配置已被其他操作修改，请刷新后重试。",
            )

        import copy

        candidate = copy.deepcopy(document)
        character = candidate.get("character_config")
        if not isinstance(character, dict):
            return self._save_result(
                ok=False,
                errors=[FieldError(field="config", message="配置缺少 character_config。")],
            )
        aemeath_config = character.setdefault("aemeath_config", {})
        if not isinstance(aemeath_config, dict):
            return self._save_result(
                ok=False,
                errors=[
                    FieldError(field="config", message="aemeath_config 结构不正确。")
                ],
            )
        aemeath_config[persona_module.PERSONA_BLOCK_KEY] = persona_module.build_persona_block(
            identity, personality, reply_style
        )

        existing = persona_module.persona_block(character.get("aemeath_config") or {})
        # Preserve the user's original wording the first time the persona is
        # migrated. ``persona_prompt`` is read by upstream as the system prompt,
        # so it has to hold the active persona; without saving the original
        # somewhere first, switching to the three fields would destroy text the
        # user wrote and make the migration irreversible. Only captured on the
        # first migration, so a later edit cannot overwrite the true original
        # with generated text.
        if not existing.get("legacy_prompt"):
            legacy = persona_module.legacy_prompt(candidate)
            if legacy:
                aemeath_config[persona_module.LEGACY_PROMPT_KEY] = legacy

        # The prompt actually used must become the split fields. Upstream reads
        # ``persona_prompt``, so leaving it stale would mean the character keeps
        # speaking with the old persona while the UI reports the new one.
        character["persona_prompt"] = persona_module.render_split(
            identity, personality, reply_style
        )

        errors = self._validate_document(candidate)
        if errors:
            return self._save_result(ok=False, errors=errors)

        ok, message = self._atomic_write(candidate)
        if not ok:
            return self._save_result(ok=False, message=message)

        logger.info("Persona updated through the management API.")
        return self._save_result(ok=True, message="已保存，重启后生效。")


__all__ = [
    "ConfigService",
    "CREDENTIAL_STORE_ENV",
    "MANAGED_API_KEY_ENV",
    "credential_is_configured",
    "credential_store_path",
    "load_credential_store",
    "save_credential",
    "CONFIG_PATH_ENV",
]
