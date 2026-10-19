"""Configs, labels, baselines and writing sets to disk."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from recon.channeldata.common import (
    CHANNELS, GRACE_DAYS, LABELS, LABEL_FILES, MARKETPLACE_SETTLEMENT_DAYS, Profile, SHIP_DAYS,
    STATEMENT_THROUGH_DAY, load_olist,
)
from recon.channeldata.assumptions import VN_CHAIN_PROFILES
from recon.channeldata.vietnam import localize_vn
from recon.channeldata.markets import MARKETS, Market
from recon.channeldata.build import build_chain, build_set


def payment_terms(market: Market) -> dict[str, dict[str, Any]]:
    """Each channel's payment terms (ADR 0002), as the market's assumptions schedule its money: a
    marketplace's release lag and payout days, the COD remittance days of the other channels."""
    a = market.assumptions
    terms = {name: {"release_days": getattr(a, f"{name}_release_days"),
                    "payout_weekdays": list(getattr(a, f"{name}_payout_weekdays")),
                    "settlement_days": MARKETPLACE_SETTLEMENT_DAYS} for name in market.reports}
    cod_days = list(getattr(a, "cod_payout_weekdays", ())) or [a.courier_payout_weekday]
    return {**terms, **{name: {"payout_weekdays": cod_days, "ship_days": SHIP_DAYS}
                        for name in CHANNELS if name not in market.reports}}


def cutoff_for(profile: Profile) -> str:
    """The set's cut-off: the last day of its statement."""
    end = pd.Timestamp(f"{profile.month}-01") + pd.DateOffset(months=1) + pd.Timedelta(days=STATEMENT_THROUGH_DAY - 1)
    return end.date().isoformat()


def config_for(market: Market, profile: Profile, cutoff: str | None = None,
               open_items: str | None = None) -> dict[str, Any]:
    """A runnable engine config for one set (cut-off: the end of its statement, unless given).
    No resolutions: the analyst works through Health."""
    feeds = {name: {"path": f"{file}.csv", "format": "csv", "encoding": "utf-8", "date_format": None}
             for name, file in (("oms", "oms_orders"), ("bank", "bank_statement"), ("logistics", "logistics_report"))}
    for name, report in market.reports.items():
        feeds[name] = {"path": report["path"], "format": "csv", "encoding": "utf-8",
                       "date_format": report["date_format"], "role": "settlement"}
    channels = {name: {"chain": "marketplace", "settlement": name, **market.reports[name]["terms"]}
                if name in market.reports else {} for name in CHANNELS}
    for name in market.cod_remittances:
        channels[name]["cod_remittances"] = True
    for name, terms in payment_terms(market).items():
        channels[name].update(terms)
    return {
        "feeds": feeds,
        "channels": channels,
        "mapping": {
            "oms": {c: c for c in ("order_id", "tracking_no", "payment_method", "order_amount", "ordered_at",
                                   "customer_name", "channel", "channel_order_no", "item_total")},
            "bank": {c: c for c in ("txn_ref", "description", "credit_amount", "fee_amount", "net_amount",
                                    "value_date")},
            "logistics": {c: c for c in ("tracking_code", "client_order_code", "cod_collected", "ship_fee",
                                         "cod_fee", "remittance_id", "status")},
            **{name: report["mapping"] for name, report in market.reports.items()},
        },
        "policy": {**market.policy,
                   "status_policy": {"delivered": ["payment", "shipment"], "shipped": ["payment", "shipment"]}},
        "period": {"cutoff": cutoff or cutoff_for(profile), "grace_days": GRACE_DAYS},
        "resolutions": [],
        **({"open_items": open_items} if open_items else {}),
    }


def labels_for(set_dir: str | Path) -> Path:
    """The labels folder of a set at `<out>/<market>/<set>[/<period>]`: `<out>/<market>/labels/<set>[/<period>]`."""
    set_dir = Path(set_dir)
    for market in (set_dir.parent, set_dir.parent.parent):
        if market.name in {m.code for m in MARKETS.values()}:
            return market / LABELS / set_dir.relative_to(market)
    raise ValueError(f"{set_dir} is not a set folder under <out>/<market>/")


def baseline(set_dir: str | Path) -> dict[str, Any]:
    """Today's engine on one set, scored on its labels, with every Health warning acknowledged so the gate can open."""
    from recon.acknowledgement import acknowledge_warnings
    from recon.evaluation import metrics
    from recon.pipeline import inspect, load_config, run

    set_dir = Path(set_dir)
    config = load_config(set_dir / "config.json")
    report = inspect(set_dir, config)
    config = acknowledge_warnings(config, report, "baseline: every warning acknowledged unread")
    outcome = run(set_dir, config, labels=labels_for(set_dir))
    if outcome.blocked:
        return {"status": "BLOCKED", "blocking": [issue.id for issue in outcome.open_issues]}
    scored = metrics(outcome.results)
    links = outcome.manifest["metrics"]["links"]
    return {"status": "COMPLETED", "warnings_acknowledged": len(config.resolutions),
            "decisions": outcome.results["decision"].value_counts().to_dict(),
            "accuracy": scored["accuracy"], "auto_accept_precision": scored["auto_accept_precision"],
            "orphans": int(len(outcome.orphans)),
            "links": {"precision": links["overall"]["precision"], "recall": links["overall"]["recall"],
                      "recall_per_link_kind": {kind: row["recall"] for kind, row in links["per_link_kind"].items()}}}


def generate(source_dir: str | Path, out_dir: str | Path, *, market: str = "vn", seed: int = 42,
             with_baseline: bool = True) -> dict[str, dict[str, Any]]:
    """Write every set of `market` to `out_dir/<market>/<profile name>/`; returns the manifests."""
    chosen = MARKETS[market]
    olist = load_olist(source_dir)
    if chosen.code == "vn":
        olist = localize_vn(olist, chosen.assumptions)
    manifests = {}
    for profile in chosen.profiles:
        frames, manifest = build_set(olist, chosen, profile, seed)
        folder = Path(out_dir) / chosen.code / profile.name
        manifests[profile.name] = _write_set(folder, frames, manifest, config_for(chosen, profile), with_baseline)
    for profile in VN_CHAIN_PROFILES if chosen.code == "vn" else ():
        previous = None
        for number, (frames, manifest) in enumerate(build_chain(olist, chosen, profile, seed), start=1):
            folder = Path(out_dir) / chosen.code / profile.name / f"period_{number}"
            cutoff = manifest["period"]["statement"][1]
            if previous is not None:  # this period's input: the open items the engine left in the last one
                frames = {**frames, OPEN_ITEMS: open_items_of(previous)}
            config = config_for(chosen, profile, cutoff, OPEN_ITEMS if previous is not None else None)
            manifests[f"{profile.name}/period_{number}"] = _write_set(folder, frames, manifest, config, with_baseline)
            previous = folder
    return manifests


OPEN_ITEMS = "open_items.csv"


def open_items_of(set_dir: Path) -> pd.DataFrame:
    """The open items the engine leaves in a set (every Health warning acknowledged)."""
    from recon.acknowledgement import acknowledge_warnings
    from recon.pipeline import inspect, load_config, run

    config = load_config(set_dir / "config.json")
    outcome = run(set_dir, acknowledge_warnings(config, inspect(set_dir, config), "chained set"))
    return outcome.open_items


def _write_set(folder: Path, frames: dict[str, pd.DataFrame], manifest: dict[str, Any], config: dict[str, Any],
               with_baseline: bool) -> dict[str, Any]:
    labels = labels_for(folder)
    for place in (folder, labels):
        place.mkdir(parents=True, exist_ok=True)
    manifest = {**manifest, "files": {}, "labels": {"folder": Path(os.path.relpath(labels, folder)).as_posix(),
                                                   "files": {}}}
    for name, frame in frames.items():
        if name in LABEL_FILES:
            path, listed = labels / name, manifest["labels"]["files"]
            (folder / name).unlink(missing_ok=True)  # sets once kept their labels inside
        else:
            path, listed = folder / name, manifest["files"]
        frame.to_csv(path, index=False)
        listed[name] = {"rows": len(frame), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (folder / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if with_baseline:
        manifest["baseline"] = baseline(folder)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    return manifest
