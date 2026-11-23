"""Run the model on the simulated venue and print what it recovers.

    python -m currentmodel.demo
"""

from __future__ import annotations

from . import VenueCorrection, cross_validate_by_day, drifts_from_records
from .geo import to_latlon
from .synthetic import (
    LAT0,
    LON0,
    BASELINE_LATE_S,
    BASELINE_ROTATION_DEG,
    BASELINE_SCALE,
    OUTFLOW,
    WIND_FACTOR,
    make_app_records,
    make_world,
)


def main() -> None:
    world = make_world()
    drifts = drifts_from_records(make_app_records(world))
    result = cross_validate_by_day(world.baseline, drifts, world.conditions)
    base, model = result["baseline"], result["model"]
    print(f"{len(drifts)} simulated drifts over {result['days']} days, each day predicted from the others\n")
    print(f"{'':24}{'baseline':>10}{'corrected':>11}")
    print(f"{'vector error, knots':24}{base['vector_rmse_kn']:>10.2f}{model['vector_rmse_kn']:>11.2f}")
    print(f"{'speed bias, knots':24}{base['speed_bias_kn']:>10.2f}{model['speed_bias_kn']:>11.2f}")
    print(f"{'direction error, deg':24}{base['direction_mae_deg']:>10.0f}{model['direction_mae_deg']:>11.0f}")
    print(f"\nskill over baseline: {result['skill']:.0%}")
    print(f"errors inside the model's 2-sigma bar: {result['within_2_sigma']:.0%} (86% expected)\n")

    fitted = VenueCorrection(world.baseline).fit(drifts, world.conditions)
    # A point in open water, away from the headland the baseline smooths out.
    lat, lon = to_latlon(-1500.0, -700.0, LAT0, LON0)
    summary = fitted.venue_summary(lat, lon)
    print(f"{'what the model learned':28}{'recovered':>10}{'simulated':>11}")
    print(f"{'baseline speed scale':28}{summary['speed_scale']:>10.2f}{1 / BASELINE_SCALE:>11.2f}")
    print(f"{'rotation, deg clockwise':28}{summary['rotation_deg']:>10.1f}{BASELINE_ROTATION_DEG:>11.1f}")
    print(f"{'baseline late, minutes':28}{summary['baseline_late_min']:>10.0f}{BASELINE_LATE_S / 60:>11.0f}")
    print(f"{'wind factor':28}{summary['wind_factor']:>10.3f}{WIND_FACTOR:>11.3f}")
    print(f"{'steady flow east, m/s':28}{summary['mean_u']:>10.3f}{OUTFLOW[0]:>11.3f}")


if __name__ == "__main__":
    main()
