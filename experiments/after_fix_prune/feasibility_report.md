# Báo cáo tính khả thi đường đi EFSM

| Kết quả | Số path |
|---|---|
| FEASIBLE | 126 |
| PROVEN_INFEASIBLE | 0 |
| SEARCH_EXHAUSTED | 0 |
| GENERATOR_ERROR | 0 |
| MODEL_ERROR | 0 |

Phạm vi chứng minh PROVEN_INFEASIBLE: every initial database state, every setup prefix placed before the path, every product choice and every request input (EFSM abstract interpretation over guards and assign actions).

| Path | Loại | Chuỗi transition | Kết quả | Bước lỗi đầu tiên | Guard / điều kiện | Context trước bước | Nguyên nhân | Chứng cứ / giới hạn | Đề xuất |
|---|---|---|---|---|---|---|---|---|---|
| P001 | positive | T10 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P002 | positive | T01>T02 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P003 | positive | T01>T03 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P004 | positive | T01>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P005 | positive | T01>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P006 | positive | T01>T04>T05>T07 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P007 | positive | T01>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P008 | positive | T01>T01>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P009 | positive | T01>T01>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P010 | positive | T01>T01>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P011 | positive | T01>T01>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P012 | positive | T01>T01>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P013 | positive | T01>T01>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P014 | positive | T01>T01>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P015 | positive | T01>T02>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P016 | positive | T01>T02>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P017 | positive | T01>T02>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P018 | positive | T01>T02>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P019 | positive | T01>T02>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P020 | positive | T01>T02>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P021 | positive | T01>T02>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P022 | positive | T01>T03>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P023 | positive | T01>T03>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P024 | positive | T01>T03>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P025 | positive | T01>T03>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P026 | positive | T01>T03>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P027 | positive | T01>T03>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P028 | positive | T01>T03>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P029 | positive | T01>T10 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P030 | positive | T01>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P031 | positive | T01>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P032 | positive | T01>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P033 | positive | T01>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P034 | positive | T02>T01>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P035 | positive | T02>T01>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P036 | positive | T02>T01>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P037 | positive | T02>T01>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P038 | positive | T02>T01>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P039 | positive | T02>T01>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P040 | positive | T02>T01>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P041 | positive | T02>T02>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P042 | positive | T02>T02>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P043 | positive | T02>T02>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P044 | positive | T02>T02>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P045 | positive | T02>T02>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P046 | positive | T02>T02>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P047 | positive | T02>T02>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P048 | positive | T02>T03>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P049 | positive | T02>T03>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P050 | positive | T02>T03>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P051 | positive | T02>T03>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P052 | positive | T02>T03>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P053 | positive | T02>T03>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P054 | positive | T02>T03>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P055 | positive | T02>T10 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P056 | positive | T02>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P057 | positive | T02>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P058 | positive | T02>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P059 | positive | T02>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P060 | positive | T02>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P061 | positive | T02>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P062 | positive | T02>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P063 | positive | T03>T01>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P064 | positive | T03>T01>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P065 | positive | T03>T01>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P066 | positive | T03>T01>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P067 | positive | T03>T01>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P068 | positive | T03>T01>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P069 | positive | T03>T01>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P070 | positive | T03>T02>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P071 | positive | T03>T02>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P072 | positive | T03>T02>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P073 | positive | T03>T02>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P074 | positive | T03>T02>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P075 | positive | T03>T02>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P076 | positive | T03>T02>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P077 | positive | T03>T03>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P078 | positive | T03>T03>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P079 | positive | T03>T03>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P080 | positive | T03>T03>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P081 | positive | T03>T03>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P082 | positive | T03>T03>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P083 | positive | T03>T03>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P084 | positive | T03>T10 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P085 | positive | T03>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P086 | positive | T03>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P087 | positive | T03>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P088 | positive | T03>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P089 | positive | T03>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P090 | positive | T03>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P091 | positive | T03>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P092 | positive | T10>T01>T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P093 | positive | T10>T01>T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P094 | positive | T10>T01>T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P095 | positive | T10>T01>T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P096 | positive | T10>T01>T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P097 | positive | T10>T01>T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P098 | positive | T10>T01>T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P099 | positive | T10>T10 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P100 | positive | T04>T05>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P101 | positive | T04>T05>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P102 | positive | T04>T05>T07>T07>T06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P103 | positive | T04>T05>T07>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P104 | positive | T04>T05>T07>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P105 | positive | T04>T05>T09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| P106 | positive | T04>T08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N001 | negative | !NT01 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N002 | negative | !NT02 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N003 | negative | !NT03 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N004 | negative | !NT04 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N005 | negative | T01>T04>T05>!NT05 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N006 | negative | T01>T04>!NT06 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N007 | negative | T01>T04>T05>T06>!NT07 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N008 | negative | T01>T04>T05>T06>!NT08 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N009 | negative | T01>T04>T08>!NT09 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N010 | negative | T01>T04>T08>!NT10 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N011 | negative | T01>T04>T08>!NT11 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N012 | negative | T01>T04>T05>!NT12 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N013 | negative | !NT13 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N014 | negative | !NT14 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N015 | negative | !NT15 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N016 | negative | !NT16 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N017 | negative | !NT17 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N018 | negative | !NT18 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N019 | negative | !NT19 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
| N020 | negative | T01>T04>T05>T06>!NT20 | FEASIBLE |  |  |  |  | CONCRETE_PLAN_FOUND_AND_GUARD_CHECKED | Không cần xử lý. |
