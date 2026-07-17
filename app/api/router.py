"""Aggregate all v1 routers under a single APIRouter."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    advisors,
    assessments,
    auth,
    availability,
    bookings,
    bookmarks,
    conversations,
    countries,
    payments,
    reviews,
    seeker_profiles,
    tickets,
    uploads,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(countries.router)
api_router.include_router(users.router)
api_router.include_router(seeker_profiles.router)
api_router.include_router(availability.router)  # before advisors: /me/availability vs /{id}
api_router.include_router(advisors.router)
api_router.include_router(bookings.router)
api_router.include_router(bookmarks.router)
api_router.include_router(payments.router)
api_router.include_router(reviews.router)
api_router.include_router(conversations.router)
api_router.include_router(assessments.router)
api_router.include_router(uploads.router)
api_router.include_router(tickets.router)
api_router.include_router(admin.router)
