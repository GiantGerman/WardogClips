"""False-positive learning: the signatures table, ticker-noise suggestions and a full rescore
(see core/learning.py)."""

from fastapi import APIRouter

from core import learning


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/learning")
    def learning_summary():
        return learning.summary(ctx.db)

    @r.post("/api/learning/rescore")
    def learning_rescore():
        res = learning.rescore(ctx.db)
        if res["rescored"]:
            ctx.bus.publish("events", action="rescored", n=res["rescored"])
        return {"ok": True, **res}

    return r
