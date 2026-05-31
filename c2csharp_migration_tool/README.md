# C → C# Migration Tool

Công cụ tự động migrate code C / Pro\*C sang C# bằng Claude API.  
Phân tích patterns và sinh ra file báo cáo CSV + file C# hoàn chỉnh.

---

## Kiến trúc – Multi-Agent Pipeline

```
Source (.c / .pc)
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  AGENT 1 – Pattern Extractor                            │
│  Quét toàn bộ source, tìm tất cả patterns C/Pro*C      │
│  Output: [{id, source_snippet, line_range, raw_type}]   │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│  AGENT 2 – Pattern Classifier                           │
│  Phân loại từng pattern theo taxonomy chuẩn             │
│  Output: +{pattern_type, sub_types, group, difficulty}  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│  AGENT 3 – C# Translator                                │
│  Dịch từng pattern sang C# idiomatric                   │
│  Output: +{csharp_snippet, summary_vi, risk, strategy}  │
└──────────┬────────────────────────────┬─────────────────┘
           │                            │
           ▼                            ▼
┌──────────────────────┐   ┌────────────────────────────┐
│  AGENT 4             │   │  AGENT 5                   │
│  C# File Generator   │   │  CSV Report Builder         │
│  Full .cs source     │   │  Pattern analysis CSV       │
└──────────────────────┘   └────────────────────────────┘
           │                            │
           ▼                            ▼
   output/<name>.cs          output/<name>_patterns.csv
```

---

## Cài đặt

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

## Sử dụng

```bash
# Migrate một file C
python main.py SjlComFunc.c

# Chỉ định thư mục output
python main.py legacy/SjlComFunc.pc --output results/
```

## Output

| File | Mô tả |
|------|-------|
| `output/<name>_migrated.cs` | File C# hoàn chỉnh, ready-to-compile |
| `output/<name>_patterns.csv` | Báo cáo phân tích tất cả patterns |
| `output/_debug_*.json` | Dữ liệu trung gian của từng agent |

---

## Cấu trúc CSV (8 trường theo spec)

| Cột | Ý nghĩa |
|-----|---------|
| No | Số thứ tự pattern |
| Pattern_C_ProC | Source code C/Pro*C gốc |
| Pattern_Type | Kiểu pattern chính |
| Pattern_SubType | Các loại kiểu con |
| Pattern_Group | Nhóm pattern |
| Summary | Thuyết minh ý nghĩa (tiếng Việt) |
| Pattern_CSharp | Source code C# tương đương |
| Difficulty | Độ khó: Dễ / Trung bình / Khó / Rất khó |
| Migration_Strategy | Phương án chuyển đổi |
| Risk_Level | Mức độ rủi ro |
| Risk_Strategy | Auto convert / Rules / AI suggest / Thủ công |
| CSharp_Popularity | Độ phổ biến x/5 |

---

## Pattern Taxonomy

| Nhóm | Ví dụ |
|------|-------|
| variable | int, char, float, static, const, extern |
| array | char[], int[], pointer |
| struct | struct, union, typedef, enum |
| memory | malloc, free, memcpy, memset |
| io | printf, scanf, fopen, fclose |
| control_flow | if/else, while, for, switch, goto |
| function | prototype, definition, call, pointer |
| preprocessor | #include, #define, #pragma |
| sql_proc | EXEC SQL, cursor, fetch (Pro*C) |
| error | errno, perror, assert |
| operator | ++/--, bitwise, pointer arithmetic |
| string | strcpy, strcmp, strlen |
| metadata | RCS tag, comment header |

---

## Cấu trúc dự án

```
c2csharp/
├── main.py                       # CLI entry point
├── pipeline.py                   # Orchestrator
├── config.py                     # Cấu hình, constants
├── agents/
│   ├── extractor_agent.py        # Agent 1: tách patterns
│   ├── classifier_agent.py       # Agent 2: phân loại
│   ├── translator_agent.py       # Agent 3: dịch sang C#
│   ├── csharp_generator_agent.py # Agent 4: sinh file .cs
│   └── report_builder_agent.py   # Agent 5: tạo CSV
├── prompts/
│   └── agent_prompts.py          # System/user prompts
├── utils/
│   ├── api_client.py             # Anthropic API wrapper
│   └── file_utils.py             # I/O helpers
├── tests/
│   └── SjlComFunc.c              # File test mẫu
└── output/                       # Kết quả sinh ra
```

---

## Risk Strategy

| Chiến lược | Khi nào dùng |
|-----------|-------------|
| Auto convert | Pattern 1:1 rõ ràng, không mất ngữ nghĩa |
| Rules theo bối cảnh | Cần biết context (ref vs out, string vs StringBuilder) |
| AI chủ động suggest | Pointer arithmetic, struct layout phức tạp |
| Làm thủ công | Pro*C / EXEC SQL, goto, setjmp/longjmp |

---

## Desktop GUI (Tkinter – chạy local, không bị proxy chặn)

```
python gui_app.py
```
hoặc trên Windows: double-click **run_gui.bat**

**Luồng hoạt động:**
1. Nhập API key vào ô trên cùng (hoặc set `ANTHROPIC_API_KEY` env var)
2. Mở file .c/.pc bằng nút "Open File…" **hoặc** paste trực tiếp vào ô source
3. Nhấn **▶ Run Migration**
4. Xem kết quả ở 3 tab: Pipeline Log / Pattern CSV / C# Output
5. Save CSV và Save .cs về máy

**Tại sao không bị proxy chặn:**
- Không dùng browser → không qua CMC Global Web Gateway
- Request đi thẳng: `python process → api.anthropic.com`
- Source code không bao giờ rời khỏi máy tính
