# Scenario artifacts

Large binary artifacts (disk images, pcaps, forensic archives) that a
scenario's manifest progressively "unlocks" into the agent's workdir via its
`artifacts:` list. Not bundled in this repo — same reasoning as
`elastic/telemetry/data/`: this is scenario content, not harness tooling,
and some of these files are many GB.

## Layout

```
scenario_artifacts/<scenario_id>/<source_path from the manifest>
```

For `scenario2_supply_chain`, the manifest's `artifacts:` list expects:
```
scenario_artifacts/scenario2_supply_chain/Step01.zip
scenario_artifacts/scenario2_supply_chain/Step02.zip
scenario_artifacts/scenario2_supply_chain/Scenario_Supply_chain_attack_Step26_combined.zip
```

Supply your own copies (or symlinks — the proctor symlinks these into the
agent's workdir itself, so a symlink-to-a-symlink works fine and avoids
duplicating multi-GB files on disk).

The proctor (`proctor_server.py`'s `_sync_artifacts`) creates a symlink for
each artifact into the run's workdir once its `unlock_at_step` is reached,
and reports it in `unlocked_artifacts`. It does not copy — some of these
files are tens of GB, and a run's workdir gets deleted after grading anyway.
