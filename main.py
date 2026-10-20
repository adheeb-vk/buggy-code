"""Application composition entry point (Milestone 10)."""

import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="reserved for Milestone 10")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger(__name__).info("not yet implemented")


if __name__ == "__main__":
    main()
