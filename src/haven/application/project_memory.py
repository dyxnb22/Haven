"""只接受显式用户操作的项目笔记；工作区身份由组合根固定。"""

from haven.contracts.project_memory import NoteAction, ProjectNote
from haven.ports.session import SessionStorePort


class ProjectMemoryService:
    def __init__(self, store: SessionStorePort, workspace: str) -> None:
        self._store = store
        self._workspace = workspace

    async def load(self) -> tuple[ProjectNote, ...]:
        return await self._store.list_project_notes(self._workspace)

    async def update(self, action: NoteAction, value: str = "") -> tuple[ProjectNote, ...]:
        if action == "remember":
            await self._store.add_project_note(self._workspace, value)
        elif action == "forget":
            if not value.isascii() or not value.isdecimal() or len(value) > 19:
                raise ValueError("usage: /memory forget NOTE_ID")
            note_id = int(value)
            if not 1 <= note_id <= 2**63 - 1:
                raise ValueError("usage: /memory forget NOTE_ID")
            if not await self._store.delete_project_note(self._workspace, note_id):
                raise ValueError("no such note in this workspace; view IDs with /memory")
        elif action == "clear":
            if value:
                raise ValueError("usage: /memory clear")
            await self._store.clear_project_notes(self._workspace)
        else:
            raise ValueError("unknown project note operation")
        return await self.load()
