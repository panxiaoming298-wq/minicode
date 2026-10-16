"""Explicit collaboration choices; no automatic expansion of worker authority."""
class ResolveCollaborationMode:
    def execute(self, settings, *, async_enabled, teams_enabled, fork_enabled, parallel_enabled, shared_enabled=False):
        role = settings.get("runtimePrimaryRole", "worker")
        team_writes = settings.get("runtimeAgentTeamWrites", False)
        if role not in {"worker", "coordinator"}:
            raise ValueError("runtimePrimaryRole must be worker or coordinator")
        if role == "coordinator" and not async_enabled:
            raise ValueError("Coordinator requires async delegation and result channels")
        if role == "coordinator" and fork_enabled:
            raise ValueError("Coordinator + Fork is unsupported; select isolated workers explicitly")
        if type(team_writes) is not bool or (team_writes and not (teams_enabled and (parallel_enabled or shared_enabled))):
            raise ValueError("runtimeAgentTeamWrites requires Teams and either parallel candidates or shared workspace writes")
        return {"policy": "parent-child-capabilities-v1", "coordinator": role == "coordinator",
            "team_writes": team_writes, "selection": "explicit_configuration"}
