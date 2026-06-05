"""
utils/call_graph.py — Cross-file Call Graph Analyzer
=====================================================
Giai đoạn 3: Phân tích caller/callee cross-file với LLM tool calling.

Luồng:
  1. build_function_index()  — regex scan toàn bộ tests/, tạo index LOCAL (không gọi API)
  2. extract_callees_local()  — regex tìm callee trong từng pattern (không gọi API)
  3. resolve_cross_file()     — LLM nhận ambiguous calls, chủ động gọi tool để tra cứu
  4. attach_to_patterns()     — gắn caller/callee vào từng pattern dict

LLM chỉ được cung cấp:
  - Danh sách ambiguous function names (không rõ từ file nào)
  - Tool: lookup_function(name)   → trả info từ index
  - Tool: lookup_callers(name)    → trả list files/lines gọi func này
  Không bao giờ nhận raw source code.
"""
