import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from device_inventory import LARGE, SMALL, parse_cpuinfo, parse_meminfo, recommend  # noqa: E402

CPUINFO = "".join(
    f"processor\t: {n}\nmodel name\t: Synthetic CPU\nflags\t\t: fpu sse4_2 avx avx2 fma\nphysical id\t: 0\ncore id\t\t: {n % 4}\n\n"
    for n in range(8))


def test_parse_cpuinfo_counts_physical_cores_and_keeps_known_flags():
    assert parse_cpuinfo(CPUINFO) == {"model": "Synthetic CPU", "physical_cores": 4, "flags": ["avx", "avx2", "fma", "sse4_2"]}


def test_parse_meminfo_reports_gib():
    text = "MemTotal:       16777216 kB\nMemAvailable:    8388608 kB\nSwapTotal:       4194304 kB\n"
    assert parse_meminfo(text) == {"total_gib": 16.0, "available_gib": 8.0, "swap_gib": 4.0}


def inventory(total=8.0, flags=("avx", "avx2"), cores=4, gpus=()):
    return {"cpu": {"physical_cores": cores, "logical": cores * 2, "flags": list(flags)},
            "memory": {"total_gib": total}, "gpus": list(gpus)}


def test_small_laptop_gets_small_model_on_cpu():
    result = recommend(inventory())
    assert (result["model"], result["gpu_offload"], result["num_thread"]) == (SMALL, "none (CPU)", 4)


def test_large_gpu_gets_7b_with_offload_and_approval_note():
    result = recommend(inventory(gpus=[{"vendor": "nvidia", "vram_gib": 6.0}]))
    assert result["model"] == LARGE and result["gpu_offload"] == "num_gpu 99"
    assert any("approval" in note for note in result["notes"])


def test_missing_avx2_and_tiny_ram_are_flagged():
    result = recommend(inventory(total=2.0, flags=("avx",), cores=2))
    assert result["model"] is None
    assert any("AVX2" in note for note in result["notes"]) and any("RAM" in note for note in result["notes"])
