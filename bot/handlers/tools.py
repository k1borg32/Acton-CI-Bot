"""
Lightweight one-shot Acton tools exposed via Telegram:

  /disasm <hex|base64>            — disassemble a BoC string
  /disasm --address <addr>        — fetch & disassemble a deployed contract
  /wrapper <owner/repo> <name>    — generate a wrapper file for a contract
  /verify <owner/repo> <name> <address>
                                  — recompile + compare bytecode hash with on-chain

These commands run in the same hardened container shape as the pipeline
but use the `run_acton_adhoc` helper instead of the build/test/check loop.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from bot.config import AppConfig
from bot.services.runner import _clone_repo, run_acton_adhoc
from bot.services.validator import parse_repo_url, ValidationError

logger = logging.getLogger(__name__)
router = Router(name="tools")

_MAX_DISASM_BYTES = 3500       # Telegram has a 4096 char limit; reserve headroom


def _force_owned(path: str, uid: int = 1000, gid: int = 1000) -> None:
    """chown path tree to (uid, gid). Tolerates failures (Windows etc.)."""
    try:
        os.chown(path, uid, gid)
    except (PermissionError, OSError, AttributeError):
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except (PermissionError, OSError, AttributeError):
                pass


def _parse_owner_repo(arg: str) -> tuple[str, str] | None:
    """Accept owner/repo or https://github.com/owner/repo."""
    arg = arg.strip()
    if arg.startswith("https://"):
        try:
            info = parse_repo_url(arg)
        except ValidationError:
            return None
        if info.platform != "github":
            return None
        return info.owner, info.repo
    if arg.count("/") == 1 and not any(c in arg for c in (";", "|", "&", "$", "`")):
        owner, repo = arg.split("/")
        if owner and repo:
            return owner, repo
    return None


def setup_tools_handler(config: AppConfig) -> Router:

    # ─────────────────── /disasm ───────────────────
    @router.message(Command("disasm"))
    async def handle_disasm(message: Message) -> None:
        if message.text is None:
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "🔬 <b>/disasm</b> — disassemble TVM bytecode.\n\n"
                "<code>/disasm &lt;hex-or-base64&gt;</code>\n"
                "<code>/disasm address &lt;ton-address&gt;</code>\n"
                "<code>/disasm address &lt;ton-address&gt; testnet</code>",
                parse_mode="HTML",
            )
            return
        arg = parts[1].strip()

        # Build the acton invocation
        if arg.lower().startswith("address "):
            rest = arg[len("address ") :].split()
            if not rest:
                await message.reply("Need an address.", parse_mode="HTML")
                return
            address = rest[0]
            net = rest[1] if len(rest) > 1 and rest[1] in ("mainnet", "testnet") else "mainnet"
            if not all(c.isalnum() or c in "-_:/" for c in address):
                await message.reply("❌ Invalid characters in address.", parse_mode="HTML")
                return
            acton_args = ["disasm", "--address", address, "--net", net]
            allow_net = True
            header = f"🔬 <code>{address}</code> on {net}\n"
        else:
            # Treat the whole arg as hex/base64 BoC
            if any(c.isspace() for c in arg) or not arg or len(arg) > 8192:
                await message.reply(
                    "❌ BoC string can't have whitespace and must be ≤8192 chars.",
                    parse_mode="HTML",
                )
                return
            acton_args = ["disasm", "--string", arg]
            allow_net = False
            header = f"🔬 BoC ({len(arg)} chars)\n"

        await message.reply("⏳ Disassembling…", parse_mode="HTML")
        try:
            code, stdout, stderr = await run_acton_adhoc(
                acton_args, config.runner,
                timeout=min(60, config.runner.build_timeout),
                allow_network=allow_net,
            )
        except Exception as e:
            logger.exception("disasm error")
            await message.reply(f"💥 Internal error: <code>{type(e).__name__}</code>", parse_mode="HTML")
            return

        if code != 0:
            err = (stderr or stdout or "")[-800:]
            await message.reply(
                f"❌ <b>disasm failed</b>\n<pre>{_escape(err)}</pre>",
                parse_mode="HTML",
            )
            return

        body = stdout.strip() or "(empty disassembly)"
        if len(body) <= _MAX_DISASM_BYTES:
            await message.reply(
                f"{header}<pre>{_escape(body)}</pre>", parse_mode="HTML"
            )
        else:
            # Too long → send as document attachment
            file = BufferedInputFile(body.encode("utf-8"), filename="disasm.tasm")
            await message.reply_document(
                document=file,
                caption=header.strip() + f" ({len(body)} chars)",
                parse_mode="HTML",
            )

    # ─────────────────── /wrapper ───────────────────
    @router.message(Command("wrapper"))
    async def handle_wrapper(message: Message) -> None:
        if message.text is None:
            return
        parts = message.text.split()
        if len(parts) < 3:
            await message.reply(
                "🛠 <b>/wrapper</b> — generate a Tolk/TS wrapper file.\n\n"
                "<code>/wrapper &lt;owner/repo&gt; &lt;ContractName&gt;</code>\n"
                "<code>/wrapper &lt;owner/repo&gt; &lt;ContractName&gt; --ts</code>",
                parse_mode="HTML",
            )
            return
        owner_repo = _parse_owner_repo(parts[1])
        if owner_repo is None:
            await message.reply("❌ Bad owner/repo argument.", parse_mode="HTML")
            return
        owner, repo = owner_repo
        contract = parts[2]
        if not contract.replace("_", "").isalnum():
            await message.reply("❌ Contract name must be alphanumeric.", parse_mode="HTML")
            return
        want_ts = "--ts" in parts[3:]

        await message.reply(
            f"⏳ Cloning <code>{owner}/{repo}</code> and generating wrapper "
            f"for <code>{contract}</code>{' (TypeScript)' if want_ts else ''}…",
            parse_mode="HTML",
        )

        tmp = tempfile.mkdtemp(prefix="acton_wrap_")
        try:
            from bot.services.validator import RepoInfo
            repo_info = RepoInfo(
                platform="github", owner=owner, repo=repo,
                url=f"https://github.com/{owner}/{repo}",
            )
            project_dir = os.path.join(tmp, "project")
            code, stdout, stderr = await _clone_repo(
                repo_info, project_dir, config.runner.clone_timeout
            )
            if code != 0:
                await message.reply(
                    f"❌ Clone failed:\n<pre>{_escape((stderr or stdout)[-400:])}</pre>",
                    parse_mode="HTML",
                )
                return

            # Ownership fix so UID-1000 runner can write back
            _force_owned(project_dir)

            out_ext = "ts" if want_ts else "tolk"
            out_name = f"{contract}.{out_ext}"
            wrapper_args = ["wrapper", contract, "--output", f"/workspace/{out_name}"]
            if want_ts:
                wrapper_args.append("--ts")

            code, stdout, stderr = await run_acton_adhoc(
                wrapper_args, config.runner,
                project_dir=project_dir,
                timeout=config.runner.build_timeout,
            )
            if code != 0:
                await message.reply(
                    f"❌ <b>wrapper failed</b>\n<pre>{_escape((stderr or stdout)[-600:])}</pre>",
                    parse_mode="HTML",
                )
                return

            out_path = os.path.join(project_dir, out_name)
            if not os.path.exists(out_path):
                await message.reply(
                    f"❌ Wrapper file not found at <code>{out_name}</code> after generation.",
                    parse_mode="HTML",
                )
                return

            with open(out_path, "rb") as f:
                data = f.read()
            await message.reply_document(
                document=BufferedInputFile(data, filename=out_name),
                caption=(
                    f"🛠 Wrapper for <b>{_escape(contract)}</b> "
                    f"in <code>{owner}/{repo}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("wrapper error")
            await message.reply(
                f"💥 Internal error: <code>{type(e).__name__}: {_escape(str(e))[:200]}</code>",
                parse_mode="HTML",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ─────────────────── /verify (lite) ───────────────────
    @router.message(Command("verify"))
    async def handle_verify(message: Message) -> None:
        if message.text is None:
            return
        parts = message.text.split()
        if len(parts) < 4:
            await message.reply(
                "🔐 <b>/verify</b> — recompile a contract and compare with on-chain bytecode.\n\n"
                "<code>/verify &lt;owner/repo&gt; &lt;ContractName&gt; &lt;ton-address&gt;</code>\n"
                "<code>/verify &lt;owner/repo&gt; &lt;ContractName&gt; &lt;addr&gt; testnet</code>\n\n"
                "<i>Lite verify: compares cell-hash, no signatures or chain submission.</i>",
                parse_mode="HTML",
            )
            return
        owner_repo = _parse_owner_repo(parts[1])
        if owner_repo is None:
            await message.reply("❌ Bad owner/repo argument.", parse_mode="HTML")
            return
        owner, repo = owner_repo
        contract = parts[2]
        if not contract.replace("_", "").isalnum():
            await message.reply("❌ Contract name must be alphanumeric.", parse_mode="HTML")
            return
        address = parts[3]
        if not all(c.isalnum() or c in "-_:" for c in address):
            await message.reply("❌ Invalid characters in address.", parse_mode="HTML")
            return
        net = parts[4] if len(parts) > 4 and parts[4] in ("mainnet", "testnet") else "mainnet"

        await message.reply(
            f"⏳ Verifying <code>{owner}/{repo}#{contract}</code> against "
            f"<code>{address}</code> on {net}…",
            parse_mode="HTML",
        )

        # 1) Fetch on-chain bytecode hash via toncenter (lite, no key needed)
        toncenter_base = (
            "https://toncenter.com/api/v2"
            if net == "mainnet"
            else "https://testnet.toncenter.com/api/v2"
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{toncenter_base}/getAddressInformation",
                    params={"address": address},
                )
                if resp.status_code != 200:
                    await message.reply(
                        f"❌ toncenter returned <code>{resp.status_code}</code>",
                        parse_mode="HTML",
                    )
                    return
                data = resp.json()
                if not data.get("ok"):
                    await message.reply(
                        f"❌ toncenter: <code>{_escape(str(data.get('error') or 'unknown'))}</code>",
                        parse_mode="HTML",
                    )
                    return
                onchain_b64 = (data.get("result") or {}).get("code") or ""
                if not onchain_b64:
                    await message.reply(
                        "❌ No code at that address (uninit account?).",
                        parse_mode="HTML",
                    )
                    return
        except httpx.HTTPError as e:
            await message.reply(
                f"❌ toncenter unreachable: <code>{_escape(str(e))[:200]}</code>",
                parse_mode="HTML",
            )
            return

        import base64
        try:
            onchain_bytes = base64.b64decode(onchain_b64)
        except Exception:
            await message.reply("❌ Couldn't decode on-chain code as base64.", parse_mode="HTML")
            return
        onchain_hash = hashlib.sha256(onchain_bytes).hexdigest()

        # 2) Clone + build locally to get the contract's BOC
        tmp = tempfile.mkdtemp(prefix="acton_verify_")
        try:
            from bot.services.validator import RepoInfo
            repo_info = RepoInfo(
                platform="github", owner=owner, repo=repo,
                url=f"https://github.com/{owner}/{repo}",
            )
            project_dir = os.path.join(tmp, "project")
            code, stdout, stderr = await _clone_repo(
                repo_info, project_dir, config.runner.clone_timeout
            )
            if code != 0:
                await message.reply(
                    f"❌ Clone failed:\n<pre>{_escape((stderr or stdout)[-400:])}</pre>",
                    parse_mode="HTML",
                )
                return
            _force_owned(project_dir)

            code, stdout, stderr = await run_acton_adhoc(
                ["build", contract], config.runner,
                project_dir=project_dir,
                timeout=config.runner.build_timeout,
            )
            if code != 0:
                await message.reply(
                    f"❌ <b>build failed</b>\n<pre>{_escape((stderr or stdout)[-600:])}</pre>",
                    parse_mode="HTML",
                )
                return

            # Find the local BOC for that contract — Acton writes them under
            # the build output dir (.acton/build/<Contract>.boc by default).
            candidates = []
            for root, _, files in os.walk(project_dir):
                for fn in files:
                    if fn.endswith(".boc"):
                        if contract.lower() in fn.lower():
                            candidates.append(os.path.join(root, fn))
            if not candidates:
                await message.reply(
                    f"❌ Couldn't find a built .boc for <code>{contract}</code>.",
                    parse_mode="HTML",
                )
                return
            local_path = candidates[0]
            with open(local_path, "rb") as f:
                local_bytes = f.read()
            local_hash = hashlib.sha256(local_bytes).hexdigest()

            match = local_hash == onchain_hash
            mark = "✅" if match else "❌"
            verdict = "match" if match else "MISMATCH"
            await message.reply(
                f"{mark} <b>{verdict}</b>\n\n"
                f"📍 <code>{address}</code> on {net}\n"
                f"📦 <code>{owner}/{repo}</code> · contract: <b>{contract}</b>\n\n"
                f"🔗 on-chain BoC sha256:\n<code>{onchain_hash}</code>\n"
                f"💻 local BoC sha256:\n<code>{local_hash}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("verify error")
            await message.reply(
                f"💥 Internal error: <code>{type(e).__name__}: {_escape(str(e))[:200]}</code>",
                parse_mode="HTML",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return router


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
