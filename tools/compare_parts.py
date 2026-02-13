#!/usr/bin/env python3
"""Compare C668119 vs C5206 to find the difference."""

test_parts = {
    "C668119": {
        "desc": "header (should use model.z=-0.134)",
        "z_min": -3.400,
        "z_max": 7.600,
        "model_z": -13.3858,
        "expected_z": -0.134,
    },
    "C5206": {
        "desc": "DIP (should use z_max≈2mm)",
        "z_min": -5.975,
        "z_max": 2.285,
        "model_z": -13.7795,
        "expected_z": 2.0,
    },
}

print("Comparing parts with matching origins:\n")
print(f"{'Metric':<30} | C668119  | C5206")
print("-" * 60)

for name, data in test_parts.items():
    print(f"\n{name}: {data['desc']}")
    print(f"{'z_min':<30} | {data['z_min']:.3f}")
    print(f"{'z_max':<30} | {data['z_max']:.3f}")
    print(f"{'model.z':<30} | {data['model_z']:.3f}")
    print(f"{'model.z / 100':<30} | {data['model_z'] / 100:.3f}")
    print(f"{'expected z-offset':<30} | {data['expected_z']:.3f}")
    print(f"{'z_max < abs(z_min)?':<30} | {data['z_max'] < abs(data['z_min'])}")
    print(f"{'z_max (extends below)':<30} | {data['z_max']:.3f}")
    print(f"{'-z_min/2 (extends above)':<30} | {-data['z_min'] / 2:.3f}")

print("\n" + "=" * 60)
print("PATTERN ANALYSIS:")
print("=" * 60)

for name, data in test_parts.items():
    extends_below = data["z_max"] < abs(data["z_min"])
    heuristic_z = data["z_max"] if extends_below else -data["z_min"] / 2
    model_z_offset = data["model_z"] / 100

    uses_heuristic = abs(heuristic_z - data["expected_z"]) < 0.3
    uses_model_z = abs(model_z_offset - data["expected_z"]) < 0.05

    print(f"\n{name}:")
    print(f"  Extends below PCB? {extends_below}")
    print(f"  Heuristic gives: {heuristic_z:.3f}")
    print(f"  model.z/100 gives: {model_z_offset:.3f}")
    print(f"  Expected: {data['expected_z']:.3f}")
    print(f"  → Uses heuristic? {uses_heuristic}")
    print(f"  → Uses model.z? {uses_model_z}")
