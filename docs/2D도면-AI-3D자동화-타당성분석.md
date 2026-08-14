# 2D 도면 → AI 분석 → SolidWorks 3D 자동화: 타당성 분석 및 로드맵

> 작성일: 2026-08-14
> 목적: 2D 도면을 AI가 분석하고 SolidWorks로 3D 모델을 자동 생성하는 프로그램의 구현 가능 범위 정리.
> 전제: 2D 도면은 AI가 최대한 많은 정보를 정확히 읽을 수 있도록 구성되어 있다고 가정.

---

## 1. 현재 코드 진단 (실행 계층 PoC 단계)

현재 리포지토리는 AI 계층 없이 SolidWorks COM 제어의 기초를 확인한 상태.

| 파일 | 내용 | 상태 |
|---|---|---|
| `draw_line.py` | 새 파트 생성 + 중심 사각형 스케치 | 동작하나 단위 버그 있음 |
| `draw_circle.py` | 정면 평면 선택 → 원 스케치 → 저장 | 가장 완성도 높음 |
| `towelShell.py` | 평면/피처 탐색 진단 스크립트 | 탐색용 |
| `test2.py` | 피처 트리 순회 및 객체 정보 출력 | 탐색용 |
| `extraction.py` | 속성·질량·재질 추출 → Excel 저장 | 동작 — **향후 검증 계층으로 재활용** |
| `dataExtraction.py` | 빈 파일 | 미구현 |

### 발견된 문제점 (수정 필요)

1. **단위 버그**: SolidWorks API는 **미터 단위** 사용. `draw_circle.py`는 `radius = 0.05`(50mm)로 올바르게 변환했지만, `draw_line.py:42`는 `width = 100`을 그대로 전달 → 100m짜리 사각형이 그려짐. 모든 입력에 `mm → m` 변환(`/1000`) 필요.
2. **3D 피처 부재**: `create_box(width, height, depth)`가 `depth`를 받지만 돌출을 호출하지 않음. `FeatureManager.FeatureExtrusion3` 호출 추가가 최우선 과제. 아직 "3D를 그리는" 코드가 없음.
3. **언어 종속성**: 평면 선택이 `"정면"`(한글판 이름)에 하드코딩. 언어 무관 동작을 위해서는 피처 트리를 순회하며 첫 번째 `RefPlane` 3개를 찾는 방식(= `towelShell.py`/`test2.py`에서 이미 실험한 방법)으로 교체.
4. **하드코딩 경로**: 템플릿 경로(`파트.prtdot`), 저장 경로가 특정 PC에 고정. `swApp.GetUserPreferenceStringValue(swUserPreferenceStringValue_e.swDefaultTemplatePart)`로 기본 템플릿 조회 가능.
5. 기타: `time.sleep` 기반 동기화 → 반환값 확인으로 교체, `SaveAs3`(구버전) → `Extension.SaveAs3` 권장, 예외 처리 표준화.

### 이미 검증된 빌딩 블록 (긍정적 자산)

- COM 연결 / 문서 생성 / 스케치 생성 (`draw_*.py`)
- 평면 선택 및 피처 트리 순회 (`towelShell.py`, `test2.py`)
- 속성·질량·부피·표면적 추출 → Excel (`extraction.py`) → **생성 결과 자동 검증 루프의 기반**

---

## 2. 파이프라인 계층별 구현 가능성

전체 구조는 4개 계층으로 나뉘며, 계층별 실현 가능성이 다름.

```
[도면 입력] → ① AI 정보 추출 → ② 3D 재구성 추론 → ③ SolidWorks 실행 → ④ 자동 검증
 (DXF/PDF/이미지)   (치수·GD&T→JSON)    (피처 시퀀스 결정)     (JSON→API 호출)     (질량·치수 대조)
```

### ① 도면 정보 추출 (AI 계층) — 조건부로 충분히 가능

- 범용 VLM(GPT-4o, Claude 등)은 GD&T·치수 파싱에서 **환각(hallucination)이 잦음**.
- 도면 400장으로 파인튜닝한 Florence-2 모델: GD&T 추출 정밀도 94.77%, F1 97.3% (arXiv 2411.03707).
- YOLOv11-obb(회전 인식 객체탐지) + VLM 하이브리드가 밀집 주석 도면에서 단일 VLM보다 우수 (arXiv 2506.17374).
- 상용 서비스 Werk24: 치수·공차·나사·GD&T·재질을 구조화 JSON으로 반환, PMI 추출 정확도 95%+ 주장. 자체 개발 전 벤치마크 대상으로 적합.
- **핵심 전략**: 도면을 래스터 이미지가 아닌 **DXF/벡터 PDF로 입력**받으면 난이도 급락.
  - `ezdxf`로 좌표·치수를 오차 없이 파싱 (숫자를 AI가 픽셀에서 읽지 않음)
  - AI에게는 "이 프로파일이 돌출인가 회전인가", "어느 뷰가 정면인가" 같은 **해석 판단만** 위임

### ② 3D 재구성 추론 (2D→3D 해석) — 부품 복잡도에 따라 갈림

| 부품 유형 | 가능성 |
|---|---|
| 2.5D 부품 (돌출·회전·구멍·패턴: 플레이트, 브래킷, 축, 플랜지) | **높음** — 닫힌 프로파일→돌출, 축대칭 단면→회전 규칙 기반 추론이 잘 동작 |
| 다중 뷰 결합 복합 부품 | 연구 단계 — Drawing2CAD(ACM MM 2025)가 벡터 도면→CAD 명령 시퀀스 생성을 시도했으나 학술 벤치마크 수준 |
| 자유곡면, 어셈블리 도면 | 현시점 자동화 부적합 |

- 2D→3D는 "제도자의 관례를 역추론해야 하는 역문제(inverse problem)"로, 완전 자동 통합 솔루션은 아직 없음.
- 상용 도구(Leo AI, Zoo, AdamCAD 등)도 주로 2.5D/스케치 기반 단순 형상을 공략 중.

### ③ SolidWorks 실행 계층 — 완전히 가능 (기술 장벽 없음)

SolidWorks COM API는 GUI에서 가능한 거의 모든 피처를 프로그래밍 가능. 순수 엔지니어링 문제.

> **⚠ 핵심 설계 결정: LLM이 SolidWorks API 코드를 직접 생성하게 하지 말 것.**
>
> - SolidWorks API 문서가 방대해 LLM이 메서드·파라미터를 환각하는 일이 흔함 (`FeatureExtrusion3`은 파라미터 23개).
> - 어셈블리 순회, 불리언 연산, COM 타입 처리에서 자주 실패한다는 실무 보고 다수.
>
> **올바른 구조**: AI는 **구조화된 JSON**(피처 목록 + 치수)만 출력 → 사전 검증된 파이썬 함수 라이브러리(`create_extrusion()`, `create_revolve()`, `create_hole()` …)가 JSON을 받아 **결정론적으로** 실행. AI의 불확실성을 "도면 해석"에만 국한시킴.

참고 구현: Prompt2CAD(LLM→pywin32), SolidWorks MCP 서버(Python 프롬프트 계층 + C# 어댑터 + COM 브리지).

### ④ 검증 계층 — 가능하며 강력 추천

- 생성 후 `extraction.py` 방식으로 질량·부피·치수 재추출 → 도면 값과 자동 대조.
- 모델 스크린샷을 VLM에게 도면과 비교시키는 2차 검증도 가능.
- 기반 코드는 이미 보유.

---

## 3. 결론: 구현 가능 범위

| 수준 | 대상 | 실현 가능성 |
|---|---|---|
| **1단계** | 잘 구조화된 도면의 2.5D 단순 부품 (돌출/회전/구멍) | **현재 기술로 완전 자동화 가능** — 수 주~수 개월 개발 규모 |
| **2단계** | 다피처 부품, 패턴·필렛·공차 반영 | **가능하나 human-in-the-loop 필수** — 추출 정확도 94~97% 수준이므로 AI 결과를 사람이 확인 후 실행하는 반자동이 현실적 |
| **3단계** | 복잡한 3면도 해석, 자유곡면, 어셈블리 | **연구 단계** — 현시점 제품화 무리 |

성패를 가르는 변수:
- "AI에게 정보를 정확히 전달하도록 구성된 도면" 전제 (핵심)
- 도면의 **DXF 등 벡터 포맷 표준화**
- 표제란·치수 표기 규칙 통일

이 조건이 갖춰지면 1단계는 확실히, 2단계도 상당 부분 도달 가능.

---

## 4. 권장 아키텍처

```
┌─────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌─────────────────┐
│ 도면 입력    │ → │ AI 해석 계층      │ → │ 피처 JSON        │ → │ 실행 계층         │
│ DXF (ezdxf) │   │ VLM/Claude/GPT   │   │ (중간 표현)       │   │ 검증된 함수 lib   │
│ PDF/이미지   │   │ 또는 Werk24 API   │   │ 스키마 검증       │   │ pywin32 COM     │
└─────────────┘   └──────────────────┘   └──────────────────┘   └────────┬────────┘
                                                                         │
                  ┌──────────────────────────────────────────────────────┘
                  ▼
          ┌──────────────────┐
          │ 검증 계층          │  질량/치수 재추출 ↔ 도면 값 대조 (extraction.py 발전형)
          │ 불일치 시 리포트    │  스크린샷 ↔ 도면 VLM 비교 (2차)
          └──────────────────┘
```

### 피처 JSON 스키마 예시 (중간 표현)

```json
{
  "part_name": "bracket_A",
  "units": "mm",
  "material": "AISI 304",
  "features": [
    {
      "type": "extrude",
      "plane": "front",
      "profile": {
        "kind": "rectangle",
        "center": [0, 0],
        "width": 100,
        "height": 50
      },
      "depth": 20,
      "direction": "blind"
    },
    {
      "type": "hole",
      "face_ref": "top_of_feature_0",
      "position": [30, 0],
      "diameter": 8,
      "through_all": true
    }
  ],
  "tolerances": [],
  "verification": { "expected_mass_kg": null, "key_dims": [] }
}
```

---

## 5. 다음 작업 순서 (로드맵)

- [ ] **Step 1 — 단위 버그 수정 + 돌출 구현**: `draw_line.py` mm→m 변환, `FeatureExtrusion3`로 스케치→3D 최초 완성
- [ ] **Step 2 — 언어 독립 평면 선택**: 피처 트리 순회로 RefPlane 3개 획득 (한글/영문판 모두 동작)
- [ ] **Step 3 — 피처 JSON 스키마 확정 + 실행기 구현**: `create_extrusion / create_revolve / create_hole / create_fillet` 함수 라이브러리, JSON → SolidWorks 결정론 실행
- [ ] **Step 4 — AI 추출 실험**: 샘플 도면 1장을 Claude/GPT vision으로 JSON화. DXF(`ezdxf`) 병행 파싱과 정확도 비교
- [ ] **Step 5 — 검증 루프**: `extraction.py` 발전시켜 생성 모델의 질량·치수를 도면 값과 자동 대조
- [ ] **Step 6 — 파일럿**: 실제 자사 도면 10~20장으로 2.5D 부품 end-to-end 성공률 측정 → human-in-the-loop UI 여부 결정

### 개발 시 주의사항 메모

- SolidWorks API 좌표·길이는 항상 **미터**, 각도는 **라디안**.
- `NewDocument` 템플릿 경로는 설치 언어/버전마다 다름 → 사용자 기본 템플릿 조회 API 사용.
- COM 호출은 반환값(bool/객체 None 여부)으로 성공 판정. `time.sleep` 의존 금지.
- LLM 호출부와 SolidWorks 실행부는 프로세스 분리 권장 (SolidWorks는 Windows 전용, LLM 호출은 어디서든 가능).
- 파라미터 많은 API(`FeatureExtrusion3` 등)는 래퍼 함수로 감싸 기본값을 고정하고, 래퍼만 테스트로 검증.

---

## 6. 참고 자료 (Sources)

### 연구 논문
- [Fine-Tuning Vision-Language Model for Automated Engineering Drawing Information Extraction (arXiv 2411.03707)](https://arxiv.org/abs/2411.03707) — Florence-2 파인튜닝, GD&T F1 97.3%
- [From Drawings to Decisions: A Hybrid Vision-Language Framework (arXiv 2506.17374)](https://arxiv.org/pdf/2506.17374) — YOLOv11-obb + VLM 하이브리드
- [Drawing2CAD: Sequence-to-Sequence Learning for CAD Generation (ACM MM 2025, arXiv 2508.18733)](https://arxiv.org/html/2508.18733v1) — 벡터 도면 → CAD 명령 시퀀스
- [Context-Aware Mapping of 2D Drawing Annotations to 3D CAD Features (arXiv 2602.18296)](https://arxiv.org/html/2602.18296v1) — 도면 주석 → 3D 피처 매핑
- [Generative AI for CAD Automation: LLMs for 3D Modelling (arXiv 2508.00843)](https://arxiv.org/pdf/2508.00843)

### 상용 도구 / 서비스
- [Werk24 — 도면 AI 추출 API (치수·GD&T·재질 → JSON, 정확도 95%+)](https://werk24.io/)
- [Leo AI — Sketch-to-CAD](https://www.getleo.ai/blog/sketch-to-cad-ai)
- [Best AI CAD Software 2026 정리 (The CAD Hub)](https://thecadhub.com/blog/smarter-cad-with-ai/)
- [Theia 2D3D — 2D→3D 변환 기술 해설](https://theia2d3d.com/insights/how-ai-converts-2d-drawings-to-3d/)

### 오픈소스 / 구현 참고
- [Prompt2CAD — 로컬 LLM으로 SolidWorks 파트 생성 (GitHub)](https://github.com/atifabid/Prompt2CAD-LLM-builds-in-solidworks)
- [SolidWorks MCP Server](https://glama.ai/mcp/servers/@arhamgarg/solidworks-mcp)
- [SolidWorks-Copilot (GitHub)](https://github.com/weianweigan/SolidWorks-Copilot)
- [ezdxf DXF 파싱 예제 (GitHub)](https://github.com/jparedesDS/extract-data-dxf)
- [ChatGPT로 SolidWorks 매크로 작성의 한계 (CadShift)](https://cadshift.com/blog/chatgpt-solidworks-macros-ai-failures/)
