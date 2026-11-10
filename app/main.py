"""단타 전략 검증기 (DayTrade Strategy Lab) - FastAPI 서버."""
from __future__ import annotations

import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine, sample

VERSION = "1.0.0"
MAX_UPLOAD = 20 * 1024 * 1024
STATIC = Path(__file__).parent / "static"

app = FastAPI(
    title="단타 전략 검증기 (DayTrade Strategy Lab)",
    version=VERSION,
    description=(
        "주식 단타 자동매매 전략이 수수료·세금·미끄러짐을 반영해도 수익이 나는 구조인지 "
        "데이터로 검증하는 API입니다. 실제 주문 기능은 없습니다."
    ),
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------------------
# 요청 모델
# ---------------------------------------------------------------------------
class CostsIn(BaseModel):
    fee_pct: float = Field(0.015, ge=0, le=1, description="매수·매도 각각 수수료 %")
    sell_tax_pct: float = Field(0.20, ge=0, le=1, description="매도 거래세 %")
    slippage_pct: float = Field(0.10, ge=0, le=2, description="매수·매도 각각 미끄러짐 %")


class BreakevenIn(BaseModel):
    take_profit_pct: float = Field(5.0, gt=0, le=50)
    stop_loss_pct: float = Field(2.0, gt=0, le=50)
    costs: CostsIn = CostsIn()


class RulesIn(BaseModel):
    stop_loss_pct: float = Field(2.0, gt=0, le=30)
    take_profit_pct: float = Field(5.0, gt=0, le=50)
    breakeven_trigger_pct: float = Field(2.5, ge=0, le=30, description="0이면 본전보호 끔")
    breakeven_offset_pct: float = Field(0.5, ge=0, le=10)
    trail_trigger_pct: float = Field(4.0, ge=0, le=30, description="0이면 추적스톱 끔")
    trail_pct: float = Field(1.5, gt=0, le=20)
    time_stop_minutes: int = Field(40, ge=0, le=390, description="0이면 시간손절 끔")
    time_stop_min_gain_pct: float = Field(1.0, ge=0, le=20)
    force_exit: str = Field("15:15", pattern=r"^\d{2}:\d{2}$")
    entry_start: str = Field("09:15", pattern=r"^\d{2}:\d{2}$")
    entry_end: str = Field("11:00", pattern=r"^\d{2}:\d{2}$")
    min_change_pct: float = Field(2.0, ge=-30, le=30)
    max_change_pct: float = Field(15.0, ge=-30, le=30)
    min_volume_ratio: float = Field(1.5, ge=0, le=50)
    max_vwap_gap_pct: float = Field(3.0, ge=0, le=30)
    max_trades_per_day: int = Field(4, ge=1, le=50)
    costs: CostsIn = CostsIn()

    def to_rules(self) -> engine.Rules:
        d = self.model_dump()
        d["costs"] = engine.Costs(**d["costs"])
        return engine.Rules(**d)


class GridIn(BaseModel):
    rules: RulesIn = RulesIn()
    stop_loss_list: list[float] = Field([1.5, 2.0, 3.0], min_length=1, max_length=10)
    take_profit_list: list[float] = Field([3.0, 5.0, 8.0], min_length=1, max_length=10)
    compare_breakeven: bool = Field(True, description="본전보호 켬/끔도 비교")


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def sample_bars() -> tuple:
    return tuple(sample.generate())


async def read_upload(f: UploadFile | None) -> str | None:
    if f is None or not f.filename:
        return None
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "파일은 20MB 이하만 올릴 수 있습니다.")
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise HTTPException(400, "CSV 인코딩을 읽을 수 없습니다(UTF-8 또는 CP949).")


async def bars_from(f: UploadFile | None) -> tuple[list[engine.Bar], str]:
    text = await read_upload(f)
    if text is None:
        return list(sample_bars()), "합성 샘플 데이터(실제 시세 아님)"
    try:
        return engine.parse_bars_csv(text), f"업로드 파일: {f.filename}"
    except ValueError as e:
        raise HTTPException(400, str(e))


def parse_json_form(raw: str | None, model):
    if not raw:
        return model()
    try:
        return model(**json.loads(raw))
    except (json.JSONDecodeError, TypeError) as e:
        raise HTTPException(400, f"설정 JSON 오류: {e}")
    except Exception as e:  # pydantic ValidationError
        raise HTTPException(422, str(e))


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health", tags=["시스템"])
def health():
    return {"status": "ok", "version": VERSION}


@app.post("/api/breakeven", tags=["1. 손익분기 계산"])
def api_breakeven(body: BreakevenIn):
    """익절·손절·비용을 넣으면 본전이 되는 최소 승률을 계산합니다."""
    try:
        return engine.breakeven(body.take_profit_pct, body.stop_loss_pct, engine.Costs(**body.costs.model_dump()))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/analyze", tags=["2. 매매기록 분석"])
async def api_analyze(file: UploadFile | None = File(None, description="매매기록 CSV (net_return_pct 열 필수)")):
    """매매기록 CSV로 승률·기대수익·손익비·MDD·청산사유별 성적을 계산합니다.
    파일을 안 올리면 합성 샘플 데이터를 기본 규칙으로 돌린 기록을 분석합니다."""
    text = await read_upload(file)
    if text is None:
        trades = engine.simulate(list(sample_bars()), engine.Rules())["trades"]
        source = "합성 샘플 데이터의 시뮬레이션 기록(실제 거래 아님)"
    else:
        try:
            trades = engine.parse_trades_csv(text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        source = f"업로드 파일: {file.filename}"
    return {"source": source, "performance": engine.performance(trades)}


@app.post("/api/simulate", tags=["3. 청산 규칙 시뮬레이션"])
async def api_simulate(
    file: UploadFile | None = File(None, description="분봉 CSV: code,datetime,open,high,low,close,volume"),
    rules: str | None = Form(None, description="RulesIn 형식 JSON 문자열 (비우면 기본값)"),
):
    """분봉 데이터에서 돌파 신호를 찾고, 손절·익절·본전보호·추적스톱·시간손절로 청산을 재현합니다."""
    r = parse_json_form(rules, RulesIn)
    bars, source = await bars_from(file)
    result = engine.simulate(bars, r.to_rules())
    result["source"] = source
    return result


@app.post("/api/grid", tags=["4. 설정 비교"])
async def api_grid(
    file: UploadFile | None = File(None, description="분봉 CSV (비우면 샘플)"),
    config: str | None = Form(None, description="GridIn 형식 JSON 문자열 (비우면 기본값)"),
):
    """손절·익절(·본전보호) 조합을 모두 돌려 기대수익 순위표를 만듭니다."""
    g = parse_json_form(config, GridIn)
    bars, source = await bars_from(file)
    try:
        rows = engine.grid(bars, g.rules.to_rules(), g.stop_loss_list, g.take_profit_list,
                           (True, False) if g.compare_breakeven else (True,))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"source": source, "results": rows}


@app.get("/api/sample/bars.csv", tags=["샘플"], response_class=PlainTextResponse)
def sample_csv():
    """분봉 CSV 형식 예시(합성 데이터)를 내려받습니다."""
    return PlainTextResponse(
        sample.to_csv(list(sample_bars())), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample_bars.csv"})


@app.get("/api/defaults", tags=["시스템"])
def defaults():
    return {"rules": RulesIn().model_dump(), "grid": GridIn().model_dump()}
