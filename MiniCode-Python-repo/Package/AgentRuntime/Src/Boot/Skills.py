"""Compose portable Skill policies and the existing SQLite file adapter."""
from dataclasses import dataclass
from ..Adapter.Out.Persistence.SkillLedger import SQLiteSkillLedger
from ..Adapter.Out.External.SkillSources import FileSkillSources
from ..Application.Usecase.SkillLifecycle import SkillLifecycle
from ..Application.Usecase.SkillMetadata import ValidateSkillMetadata
from ..Application.Usecase.SkillCatalog import BuildSkillCatalog, DiscoverSkills, SELECTION_GUIDANCE
from ..Application.Usecase.SkillLint import LintSkill


@dataclass(frozen=True, slots=True)
class RunnableApp:
    def selection_guidance(self):
        return SELECTION_GUIDANCE

    def metadata(self, value, **options):
        return ValidateSkillMetadata().execute(value, **options)

    def catalog(self, skills, **options):
        return BuildSkillCatalog().execute(skills, **options)

    def discover(self, skills, query="", **options):
        return DiscoverSkills().execute(skills, query, **options)

    def lifecycle(self, *, database, lookup, authorize, owner, attempt, available_tools, retained_rules=False):
        return SkillLifecycle(ledger=SQLiteSkillLedger(database), sources=FileSkillSources(lookup, authorize), owner=owner,
                              attempt=attempt, available_tools=available_tools, retained_rules=retained_rules)

    def release_run(self, database, **scope):
        return SQLiteSkillLedger(database).release_run(**scope)

    def lint(self, name, *, lookup, authorize):
        return LintSkill(FileSkillSources(lookup, authorize)).execute(name)

    def audit(self, database, *, workspace, invocation_id):
        return SQLiteSkillLedger(database).audit(workspace=workspace, invocation_id=invocation_id)


def createApp():
    return RunnableApp()
