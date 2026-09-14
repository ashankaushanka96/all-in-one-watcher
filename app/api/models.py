from typing import List, Optional

from pydantic import BaseModel, Field


class ComponentConfigEntry(BaseModel):
    """One config.ini section, as submitted by the dashboard's Configure Watcher UI."""

    section_id: str
    tag: str
    name: str
    port: Optional[int] = None
    startTime: str
    endTime: str
    runningDates: List[int] = Field(default_factory=list)
    maxUpDays: int = 1
    needToUp: bool = False
    needToSendMail: bool = False
    runScriptPath: str
    runScript: str
    logDirectory: Optional[str] = None


class ConfigureRequest(BaseModel):
    add_components: List[ComponentConfigEntry] = Field(default_factory=list)
    remove_component_ids: List[str] = Field(default_factory=list)
    restart: bool = True


class RollbackRequest(BaseModel):
    restart: bool = True


class ComponentActionRequest(BaseModel):
    """Identifies a component by its config.ini tag (the value component_watcher.py's pgrep match uses)."""

    tag: str
