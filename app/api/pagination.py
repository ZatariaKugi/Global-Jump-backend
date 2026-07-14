"""Offset/page-based pagination — query params, a DB helper, and a meta builder."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Annotated, Any

from fastapi import Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.response import Meta, PageMeta

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(slots=True)
class PaginationParams:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def pagination_params(
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page")
    ] = DEFAULT_PAGE_SIZE,
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


PaginationDep = Annotated[PaginationParams, Depends(pagination_params)]


async def paginate(
    session: AsyncSession, stmt: Select[Any], params: PaginationParams
) -> tuple[list[Any], int]:
    """Return ``(items, total_count)`` for ``stmt`` at the requested page.

    The count is computed from the statement (ordering stripped) so it stays correct
    regardless of the underlying filters.
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.scalar(count_stmt)) or 0
    result = await session.execute(stmt.offset(params.offset).limit(params.limit))
    return list(result.scalars().all()), total


def page_meta(params: PaginationParams, total: int, request_id: str | None = None) -> Meta:
    pages = ceil(total / params.page_size) if total else 0
    return Meta(
        request_id=request_id,
        pagination=PageMeta(
            total=total,
            page=params.page,
            page_size=params.page_size,
            pages=pages,
        ),
    )
