"""Capability profiles: what a scenario needs beyond raw Bash+Elasticsearch
access, and a preflight check so a missing-tool failure surfaces as a clear
message before spending any model budget, not as a confusing mid-run error.

Enforcement here is intentionally light (a `shutil.which`/`import` check on
the host, run once before the job) rather than a hard sandbox boundary --
matching this harness's current process-level isolation. A future Docker
phase would instead pick a distinct image per profile, so "elastic-only"
scenarios never pay for the forensics image's size/build time; the profile
names here are written to map directly onto that later.
"""
import importlib
import shutil

PROFILES = {
    "elastic-only": {
        "description": "Bash + Elasticsearch/Kibana queries only.",
        "cli_tools": [],
        "python_modules": [],
    },
    "elastic-forensics": {
        "description": (
            "Disk-image / binary-forensics scenarios (see bizone-benchmarks/work_step26/ "
            "for what this is grounded in): Sleuthkit for image/MFT parsing, UPX for "
            "unpacking, hashcat/john for hash and key brute-forcing, pefile/objdump/xxd "
            "for PE inspection, python-evtx/regipy for Windows event log and registry "
            "parsing recovered from a disk image."
        ),
        "cli_tools": ["mmls", "fls", "icat", "istat", "blkls", "blkcat", "upx", "objdump", "xxd", "hashcat"],
        "python_modules": ["pytsk3", "regipy", "pefile", "Evtx.Evtx"],
        "brew_install": "brew install sleuthkit upx hashcat",
        "pip_install": "pip3 install --user pytsk3 regipy pefile python-evtx",
    },
}


class CapabilityError(RuntimeError):
    pass


def check(profile_name):
    """Returns (ok, missing_tools, missing_modules). Never raises for an
    unknown profile name -- treated as needing nothing extra, so a typo in a
    manifest fails on "unknown scenario" elsewhere, not silently here."""
    profile = PROFILES.get(profile_name)
    if not profile:
        return True, [], []

    missing_tools = [t for t in profile["cli_tools"] if not shutil.which(t)]

    missing_modules = []
    for mod in profile["python_modules"]:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_modules.append(mod)

    return (not missing_tools and not missing_modules), missing_tools, missing_modules


def require(profile_name):
    ok, missing_tools, missing_modules = check(profile_name)
    if ok:
        return
    profile = PROFILES[profile_name]
    msg = ["Capability profile %r is missing:" % profile_name]
    if missing_tools:
        msg.append("  CLI tools: %s" % ", ".join(missing_tools))
    if missing_modules:
        msg.append("  Python modules: %s" % ", ".join(missing_modules))
    if profile.get("brew_install"):
        msg.append("Install with: %s" % profile["brew_install"])
    if profile.get("pip_install"):
        msg.append("            and: %s" % profile["pip_install"])
    raise CapabilityError("\n".join(msg))
