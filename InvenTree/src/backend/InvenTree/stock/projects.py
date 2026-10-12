"""Resolve warehouse project selections against the shared unique project archive."""

# Business messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

from rest_framework.exceptions import ValidationError

from common.models import ProjectCode


def selected_project(values, *, required=True, allow_inactive=False):
    """Accept an archive ID or an existing exact code, never silently create a project."""
    number = values.get('project')
    project_id = values.get('project_id')
    query = ProjectCode.objects.all()
    if not allow_inactive:
        query = query.filter(active=True)
    if project_id is not None:
        project = query.filter(pk=project_id).first()
    elif number:
        project = query.filter(code=number.strip()).first()
    elif not required:
        return None
    else:
        raise ValidationError({'project_id': '请选择项目；没有对应项目时请先新建'})
    if project is None:
        raise ValidationError(
            {'project_id': '项目不存在或已停用，请先新建或选择有效项目'}
        )
    if number is not None and number.strip() != project.code:
        raise ValidationError({'project': '项目号与所选项目不一致'})
    return project


def bind_project(values, *, required=True):
    """Store a stable foreign key while retaining the printable project-code snapshot."""
    project = selected_project(values, required=required)
    values.pop('project_id', None)
    if project is not None:
        values.update(project=project.code, project_code=project)
    return values
