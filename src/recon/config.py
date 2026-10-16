"""Run config: the single, serializable source of truth for one run."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from recon.health import CONVERT_CURRENCY, SET_STATUS_POLICY
from recon.schema import DEFAULT_ROLES, FEEDS, ROLES, SETTLEMENT

# Evidence an order status can require before its unit ties out.
PAYMENT, SHIPMENT = "payment", "shipment"
EVIDENCE = (PAYMENT, SHIPMENT)


def _default_status_policy() -> dict[str, tuple[str, ...]]:
    return {"delivered": EVIDENCE, "shipped": EVIDENCE}


@dataclass(frozen=True)
class FxPolicy:
    """How a credit in another currency than its order is converted into the order currency.

    A rate is order-currency units per credit-currency unit. `rate_column` (a bank feed
    column) wins where it has a value; otherwise `rate_table` (CSV: date, from, to, rate,
    relative to the sources) gives the latest rate on or before the value date.
    """

    rate_column: str | None = None
    rate_table: str | None = None
    tolerance: float = 0.01  # relative; converted differences within it are FX_DIFFERENCE

    def __post_init__(self) -> None:
        if not (self.rate_column or self.rate_table):
            raise ValueError("FX needs a rate source: rate_column and/or rate_table")


@dataclass(frozen=True)
class Policy:
    accept_threshold: float = 0.995
    mdr_bps: int = 180  # fee rate of a prepaid credit whose payment type has no rate of its own
    # bank payment_type -> fee rate in bps, e.g. {"bank_transfer": 0, "wallet": 110}
    payment_type_fee_bps: dict[str, int] = field(default_factory=dict)
    cod_fee_bps: int = 80
    amount_tolerance: float = 0.01  # absolute, in the feed currency
    link_date_window_days: int = 4  # linking pass 2: |value_date - order date| allowed
    # Without a stated interest, an installment credit up to this share above the order is
    # POSSIBLE_INSTALLMENT_INTEREST (REVIEW) rather than AMOUNT_MISMATCH.
    installment_interest_max: float = 0.5
    # order status -> the EVIDENCE it requires; unlisted statuses are pending policy
    status_policy: dict[str, tuple[str, ...]] = field(default_factory=_default_status_policy)
    fx: FxPolicy | None = None  # None: credits in another currency than their order are not compared
    # Regex with a named group `payer` that finds the payer in a bank description, for feeds without a
    # payer_name column. None: credits have no payer name, and pass 2 ignores who paid.
    payer_pattern: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.fx, dict):
            object.__setattr__(self, "fx", FxPolicy(**self.fx))
        unknown = {e for required in self.status_policy.values() for e in required} - set(EVIDENCE)
        if unknown:
            raise ValueError(f"unknown evidence in status_policy: {sorted(unknown)}")
        normalized = {str(status): tuple(required) for status, required in self.status_policy.items()}
        object.__setattr__(self, "status_policy", normalized)
        if self.payer_pattern is not None and "payer" not in re.compile(self.payer_pattern).groupindex:
            raise ValueError("payer_pattern needs a named group 'payer', e.g. (?P<payer>[A-Z ]+)")
        rates = {str(kind): int(bps) for kind, bps in self.payment_type_fee_bps.items()}
        object.__setattr__(self, "payment_type_fee_bps", rates)

    def fee_bps(self, payment_type: str) -> int:
        """The fee rate for a credit of `payment_type`: its own rate, else `mdr_bps`."""
        return self.payment_type_fee_bps.get(payment_type, self.mdr_bps)

    def __hash__(self) -> int:
        return hash(tuple((f, repr(getattr(self, f))) for f in self.__dataclass_fields__))


@dataclass(frozen=True)
class FeedSpec:
    """Where one feed lives. `path` is free-form, relative to the sources folder.

    `role` is one of ROLES (orders, bank, logistics, settlement); a feed named oms, bank or
    logistics has that role, so only a settlement report needs to state it.
    """

    path: str
    format: str = "csv"
    encoding: str = "utf-8"
    date_format: str | None = None
    delimiter: str | None = None  # CSV only; None = detect from the header line
    role: str | None = None

    def __post_init__(self) -> None:
        if self.role is not None and self.role not in ROLES:
            raise ValueError(f"unknown feed role {self.role!r}; expected one of {', '.join(ROLES)}")


# Money chains (ADR 0001): how a channel's money reaches the bank.
PREPAID, MARKETPLACE = "prepaid", "marketplace"
CHAINS = (PREPAID, MARKETPLACE)


@dataclass(frozen=True)
class Channel:
    """One sales channel (an OMS `channel` value) and its money chain.

    prepaid      order ──order ref──▶ bank credit. `order_ref_pattern` finds the order in a bank
                 description: its named group `ref`, else its first group, else the whole match.
                 None: the default pattern.
    marketplace  order ──channel order no──▶ settlement line ──payout id──▶ bank credit.
                 `settlement` names the settlement report feed; `fee_bps` is the channel's total
                 fee on a line's gross (None: fees are not checked).

    `amount_tolerance` (None: the policy's) applies to the channel's line checks and batch tie-out.
    """

    chain: str = PREPAID
    order_ref_pattern: str | None = None
    settlement: str | None = None
    fee_bps: int | None = None
    amount_tolerance: float | None = None

    def __post_init__(self) -> None:
        if self.chain not in CHAINS:
            raise ValueError(f"unknown money chain {self.chain!r}; expected one of {', '.join(CHAINS)}")
        if self.order_ref_pattern is not None:
            re.compile(self.order_ref_pattern)
        if (self.chain == MARKETPLACE) != (self.settlement is not None):
            raise ValueError("a marketplace channel names its settlement report (settlement); "
                             "other chains have none")

    def tolerance(self, policy: Policy) -> float:
        return policy.amount_tolerance if self.amount_tolerance is None else self.amount_tolerance


@dataclass(frozen=True)
class Resolution:
    """An analyst's decision on one Health Report issue."""

    issue_id: str
    option: str
    params: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class RunConfig:
    feeds: dict[str, FeedSpec]
    mapping: dict[str, dict[str, str]] = field(default_factory=dict)  # feed -> {canonical: source column}
    policy: Policy = Policy()
    resolutions: tuple[Resolution, ...] = ()
    ground_truth: str | None = None  # optional truth CSV, relative to the sources folder
    link_truth: str | None = None  # optional true links CSV (feed, record_key, order_id[, link_kind])
    # OMS `channel` value -> its channel. None declared: every order is on one default prepaid chain.
    channels: dict[str, Channel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, spec in self.feeds.items():
            if name in FEEDS:
                if spec.role not in (None, DEFAULT_ROLES[name]):
                    raise ValueError(f"feed {name!r} has the {DEFAULT_ROLES[name]} role, not {spec.role!r}")
            elif spec.role != SETTLEMENT:
                raise ValueError(f"feed {name!r} needs a role: the orders, bank and logistics feeds are named "
                                 f"{', '.join(FEEDS)}; any other feed is a settlement report (role "
                                 f"{SETTLEMENT!r})")
        for name, channel in self.channels.items():
            if channel.settlement is not None and self.role(channel.settlement) != SETTLEMENT:
                raise ValueError(f"channel {name!r}: {channel.settlement!r} is not a settlement report feed")

    def role(self, feed: str) -> str | None:
        """The feed's role: implied by its name (oms, bank, logistics), else as its spec states."""
        spec = self.feeds.get(feed)
        return DEFAULT_ROLES.get(feed) or (spec.role if spec else None)

    def kind(self, feed: str) -> str | None:
        """The schema kind of the feed's role (see ``schema.ROLES``): the key of every schema table."""
        role = self.role(feed)
        return ROLES[role] if role else None

    @property
    def marketplaces(self) -> dict[str, Channel]:
        """The channels on the marketplace chain, by OMS channel value."""
        return {name: c for name, c in self.channels.items() if c.chain == MARKETPLACE}

    @property
    def order_ref_patterns(self) -> tuple[str, ...]:
        """The prepaid chains' own patterns for an order reference in a bank description, deduplicated."""
        own = [c.order_ref_pattern for c in self.channels.values() if c.chain == PREPAID and c.order_ref_pattern]
        return tuple(dict.fromkeys(own))

    @property
    def resolved_policy(self) -> Policy:
        """`policy` with the status evidence and FX setting that resolutions chose (the last FX one wins)."""
        policy = self.policy
        chosen = {r.params["status"]: tuple(r.params["evidence"])
                  for r in self.resolutions if r.option == SET_STATUS_POLICY}
        if chosen:
            policy = replace(policy, status_policy={**policy.status_policy, **chosen})
        fx = [r.params for r in self.resolutions if r.option == CONVERT_CURRENCY]
        if fx:
            policy = replace(policy, fx=FxPolicy(**{k: fx[-1].get(k) for k in ("rate_column", "rate_table")},
                                                 tolerance=float(fx[-1].get("tolerance", 0.01))))
        return policy

    def date_format(self, feed: str) -> str | None:
        """The feed's declared strftime date format, if any."""
        spec = self.feeds.get(feed)
        return spec.date_format if spec else None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["resolutions"] = [asdict(r) for r in self.resolutions]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        return cls(
            feeds={name: FeedSpec(**spec) for name, spec in data["feeds"].items()},
            mapping={feed: dict(cols) for feed, cols in data.get("mapping", {}).items()},
            policy=Policy(**data.get("policy", {})),
            resolutions=tuple(Resolution(**r) for r in data.get("resolutions", [])),
            ground_truth=data.get("ground_truth"),
            link_truth=data.get("link_truth"),
            channels={name: Channel(**spec) for name, spec in data.get("channels", {}).items()},
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def load_config(path: str | Path) -> RunConfig:
    """Read a run config from JSON or (with PyYAML installed) YAML."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        return RunConfig.from_dict(yaml.safe_load(text))
    return RunConfig.from_dict(json.loads(text))


def save_config(config: RunConfig, path: str | Path) -> None:
    path = Path(path)
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        path.write_text(yaml.safe_dump(config.to_dict(), sort_keys=True), encoding="utf-8")
    else:
        path.write_text(config.to_json(), encoding="utf-8")
