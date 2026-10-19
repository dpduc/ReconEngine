"""python -m recon.channeldata"""

import argparse

from recon.channeldata import MARKETS, generate

parser = argparse.ArgumentParser(prog="python -m recon.channeldata")
parser.add_argument("--source", default="brazil ecom")
parser.add_argument("--out", default="test")
parser.add_argument("--market", choices=sorted(MARKETS), default="vn")
parser.add_argument("--seed", type=int, default=42)
parser.add_argument("--no-baseline", action="store_true", help="skip running today's engine on each set")
args = parser.parse_args()
for name, manifest in generate(args.source, args.out, market=args.market, seed=args.seed,
                               with_baseline=not args.no_baseline).items():
    print(name, manifest["units"], manifest["orders_by_channel"], manifest.get("baseline"))
