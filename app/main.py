"""野外调查观测更正汇交 API。

多台离线设备对同一观测的更正在此汇交：修订 DAG 乱序到达、待补齐自动重算、
多分支冲突、人工裁决、幂等重传、整包拒绝。双进程共库（SQLite WAL）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import AliasChoices, BaseModel, Field, model_validator

from . import assessment, service
from .db import init_db, read_tx, snapshot_tx, write_tx


# ------------------------------------------------------------------ 模型

class PayloadFields(BaseModel):
    """修订载荷：物种+数量 或 删除标记，二者互斥。"""

    species: Optional[str] = Field(default=None, min_length=1, max_length=256)
    count: Optional[int] = Field(default=None, ge=0)
    delete: bool = False

    @model_validator(mode="after")
    def _check_payload(self):
        if self.delete:
            if self.species is not None or self.count is not None:
                raise ValueError("删除标记与物种/数量互斥，不能同时携带")
        else:
            if self.species is None or self.count is None:
                raise ValueError("非删除修订必须携带 species 与 count")
        return self


class RevisionIn(PayloadFields):
    observation_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    parents: list[str] = Field(default_factory=list)


class PackageIn(BaseModel):
    revisions: list[RevisionIn] = Field(min_length=1)


class ResolutionIn(PayloadFields):
    """人工裁决：parents 必须等于裁决时所见全部分支（当前 tips）。"""

    revision_id: str = Field(min_length=1, max_length=128)
    parents: list[str] = Field(default_factory=list)


class EffectiveValue(BaseModel):
    revision_id: str
    species: Optional[str]
    count: Optional[int]
    deleted: bool


class ObservationState(BaseModel):
    observation_id: str
    status: str = Field(description="pending=待补齐 / valid=有效 / conflict=冲突")
    tips: list[str] = Field(description="当前分支（无后继的修订）")
    missing_ancestors: list[str] = Field(description="未齐祖先（待补齐的修订号）")
    effective: Optional[EffectiveValue] = Field(description="有效值；待补齐或冲突时为 null")
    version: int


class PackageResult(BaseModel):
    accepted: list[str] = Field(description="本次新入库的修订号")
    duplicates: list[str] = Field(description="同号同内容幂等跳过的修订号")
    observations: list[ObservationState] = Field(description="受影响观测重算后的状态")


class RevisionOut(BaseModel):
    revision_id: str
    observation_id: str
    parents: list[str]
    species: Optional[str]
    count: Optional[int]
    delete: bool
    received_at: str


# ------------------------------------------------------------ 判读模型

class AssessIn(BaseModel):
    """判读请求：最多 50 个不重复观测号 + 目标物种 + 0～100 的整数百分比阈值。"""

    model_config = {"populate_by_name": True}

    observation_ids: list[str] = Field(
        validation_alias=AliasChoices("observation_ids", "ids"),
        min_length=1, max_length=50, description="观测号，最多 50 个且不重复",
    )
    species: str = Field(min_length=1, max_length=256, description="目标物种")
    threshold_percent: int = Field(
        validation_alias=AliasChoices(
            "threshold_percent", "threshold", "percent"),
        ge=0, le=100, description="预警线（整数百分比），严格大于才算超标",
    )

    @model_validator(mode="after")
    def _check_ids(self):
        for obs_id in self.observation_ids:
            if not obs_id or len(obs_id) > 128:
                raise ValueError("观测号长度须为 1～128")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("观测号不得重复")
        return self


class BlockReason(BaseModel):
    code: str = Field(description=(
        "observation_not_found=观测不存在 / observation_pending=待补齐 / "
        "zero_batch_total=某种选择使整批总数为零 / branch_unavailable=分支不可用"
    ))
    observation_id: Optional[str]
    message: str


class AssessedObservation(BaseModel):
    observation_id: str
    status: str = Field(description="pending=待补齐 / valid=有效 / conflict=冲突")
    tips: list[str] = Field(description="当前分支（无后继修订）")
    missing_ancestors: list[str]
    version: int


class Selection(BaseModel):
    observation_id: str
    revision_id: str
    species: Optional[str]
    count: Optional[int]
    deleted: bool
    species_count: int = Field(description="该分支计入的目标物种个体数")
    total_count: int = Field(description="该分支计入的整批总数")


class BranchGroup(BaseModel):
    revision_ids: list[str] = Field(description="每个观测所选分支，按请求顺序")
    selections: list[Selection]
    species_count: int
    total_count: int
    ratio_percent: Optional[float]
    exceeds: bool


class Witnesses(BaseModel):
    exceeding: BranchGroup = Field(description="一组支持“超标”的分支选择")
    not_exceeding: BranchGroup = Field(description="一组支持“未超标”的分支选择")
    exceeding_revision_ids: list[str] = Field(
        description="支持“超标”的一组修订号（按请求顺序，每个观测一条）"
    )
    not_exceeding_revision_ids: list[str] = Field(
        description="支持“未超标”的一组修订号（按请求顺序，每个观测一条）"
    )


class AssessOut(BaseModel):
    verdict: Optional[str] = Field(
        description="确定超标 / 确定未超 / 待裁决；不给判定时为 null"
    )
    code: str = Field(description=(
        "exceeds / not_exceeds / undecided / blocked"
    ))
    species: str
    threshold: int = Field(description="本次判读使用的预警线（整数百分比）")
    threshold_percent: int = Field(description="同 threshold")
    observation_ids: list[str]
    reasons: list[BlockReason] = Field(description="不给判定的原因；正常判读为空")
    observations: list[AssessedObservation] = Field(
        description="本次判读所用观测状态（未找到的观测不在此列）"
    )
    witnesses: Optional[Witnesses] = Field(
        description="待裁决时各一组支持超标/未超标的修订号"
    )


# ------------------------------------------------------------------ 应用

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="野外调查观测更正汇交 API",
    version="1.0.0",
    description=(
        "汇合多台离线设备对同一观测的更正：修订按 DAG 组织，允许子先父后乱序到达；"
        "祖先未齐标 **pending**（不出有效值），补齐自动重算；单分支为 **valid**；"
        "多分支（含改值与删除并存）为 **conflict**，需人工裁决。"
        "裁决以所见全部分支为父，提交时分支已变返回 409；"
        "同号同内容重传幂等，异内容/循环/跨观测引用整包拒绝。"
        "**判读** `/assessments`：对最多 50 个观测按当前分支覆盖全部可行选择，"
        "给出确定超标 / 确定未超 / 待裁决（附两组见证修订号），"
        "或在观测不存在 / 待补齐 / 存在总数为零的选择时列出原因、不给判定。"
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/docs")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


def _to_payload(r: RevisionIn) -> service.RevisionPayload:
    return service.RevisionPayload(
        observation_id=r.observation_id,
        revision_id=r.revision_id,
        parents=r.parents,
        species=r.species,
        count=r.count,
        delete=r.delete,
    )


@app.post("/packages", response_model=PackageResult, tags=["汇交"])
def submit_package(pkg: PackageIn):
    """上报一个 JSON 包（可含多条修订、多个观测）。整体原子：任一非法则整包拒绝。"""
    try:
        with write_tx() as conn:
            return service.submit_package(conn, [_to_payload(r) for r in pkg.revisions])
    except service.PackageRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/observations", response_model=list[ObservationState], tags=["查询"])
def list_observations(
    status: Optional[str] = Query(default=None, pattern="^(pending|valid|conflict)$")
):
    """列出全部观测状态，可按 status 过滤。"""
    with read_tx() as conn:
        return service.list_observations(conn, status)


@app.get("/conflicts", response_model=list[ObservationState], tags=["查询"])
def list_conflicts():
    """查冲突：返回全部多分支观测及其当前分支（tips），供裁决取数。"""
    with read_tx() as conn:
        return service.list_observations(conn, "conflict")


@app.get("/observations/{observation_id}", response_model=ObservationState, tags=["查询"])
def get_observation(observation_id: str):
    """查单个观测：状态、当前分支、未齐祖先、有效值。"""
    with read_tx() as conn:
        state = service.get_observation(conn, observation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="观测不存在")
    return state


@app.get(
    "/observations/{observation_id}/revisions",
    response_model=list[RevisionOut],
    tags=["查询"],
)
def get_observation_revisions(observation_id: str):
    """查观测的全部修订（DAG 节点）。"""
    with read_tx() as conn:
        state = service.get_observation(conn, observation_id)
        if state is None:
            raise HTTPException(status_code=404, detail="观测不存在")
        return service.list_revisions(conn, observation_id)


@app.post("/assessments", response_model=AssessOut, tags=["判读"])
@app.post("/assess", response_model=AssessOut, include_in_schema=False)
def assess(body: AssessIn):
    """判读物种个体数占比是否超过预警线。

    每条冲突观测只取一条当前分支；目标物种数与整批总数来自同一组分支选择。
    删除分支两项均计零，其他物种只计总数。返回：

    - **确定超标**：所有可行选择都超标；
    - **确定未超**：所有可行选择都未超标（等于阈值不算超标）；
    - **待裁决**：有的选择超标、有的未超标，给出各一组见证修订号；
    - **不给判定**：观测不存在 / 待补齐 / 某种选择使整批总数为零，列出原因。

    判定与所用分支在同一个只读快照内读取，保证来自同一已提交状态。
    """
    with snapshot_tx() as conn:
        return assessment.assess(
            conn, body.observation_ids, body.species, body.threshold_percent
        )


@app.post(
    "/observations/{observation_id}/resolutions",
    response_model=ObservationState,
    status_code=201,
    tags=["裁决"],
)
def resolve_observation(observation_id: str, body: ResolutionIn, response: Response):
    """人工裁决：以所见全部分支为父新建修订。分支已变返回 409 及当前分支。"""
    rev = service.RevisionPayload(
        observation_id=observation_id,
        revision_id=body.revision_id,
        parents=body.parents,
        species=body.species,
        count=body.count,
        delete=body.delete,
    )
    try:
        with write_tx() as conn:
            state, replayed = service.resolve_observation(conn, observation_id, rev)
    except service.ObservationNotFound:
        raise HTTPException(status_code=404, detail="观测不存在")
    except service.BranchesChanged as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "提交时分支已变化，请按当前分支重新裁决",
                "current_tips": exc.current_tips,
                "status": exc.status,
            },
        )
    except service.PackageRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if replayed:  # 同号同内容重传：幂等返回
        response.status_code = 200
    return state
