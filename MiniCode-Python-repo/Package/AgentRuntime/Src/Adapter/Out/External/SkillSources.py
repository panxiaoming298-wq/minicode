"""Read bounded, ordinary package resources through the host permission seam."""
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from ....Application.Dto.Skill import SkillResource
from ....Application.Port.Out.SkillStorage import SkillSources


class FileSkillSources(SkillSources):
    def __init__(self, lookup, authorize):
        self.lookup, self.authorize = lookup, authorize

    def load(self, name):
        return self.lookup(name)

    def validate_resource(self, document, path):
        if not isinstance(path, str) or not path or "\\" in path or ":" in path:
            raise ValueError("Skill resource must be a package-relative POSIX path")
        relative = PurePosixPath(path)
        if relative.is_absolute() or PureWindowsPath(path).is_absolute() or ".." in relative.parts or len(relative.parts) > 8:
            raise ValueError("Skill resource escapes its package or exceeds depth 8")
        root = Path(document.package_root).resolve(strict=True)
        target = root.joinpath(*relative.parts)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Skill resource aliases are unsupported")
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or not target.resolve().is_relative_to(root):
            raise ValueError("Skill resource must be an ordinary file inside its package")
        self.authorize(target)
        return target

    def resource(self, document, path):
        target = self.validate_resource(document, path)
        canonical = target.resolve()
        if canonical == Path(document.path).resolve():
            raise ValueError("Required resources must not include SKILL.md itself; its body is already mandatory")
        with target.open("rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError("Skill resource exceeds 256 KiB; split it into explicit branches")
        return SkillResource(canonical.relative_to(Path(document.package_root).resolve()).as_posix(),
                             raw.decode("utf-8-sig"), hashlib.sha256(raw).hexdigest())
