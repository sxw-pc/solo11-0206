# 纸机换卷与纸幅分切索赔采录台

车间质量员采录纸机计数段、母卷、换卷搭接和断纸，分切员记录子卷刀位、实剪段和复称，售后人员核算客户取样能归到哪一张当前有效的子卷签发凭证。离线使用 Python 3.11+ 标准库与本地 SQLite，无安装依赖。

```sh
python -B -m paper_web --db paper.sqlite demo fixtures/demo.json
python -B -m paper_web --db paper.sqlite reel R2
python -B -m paper_web --db paper.sqlite scan D-cross
python -B -m paper_web --db paper.sqlite plan C
python -B -m paper_web --db paper.sqlite finish C
python -B -m paper_web --db paper.sqlite claim claim-7
python -B -m paper_web --db paper.sqlite recompute
python -B -m paper_web --db paper.sqlite ingest fixtures/correction.json
python -B -m paper_web --db paper.sqlite recompute
python -B -m unittest discover -s tests -v
```

`demo` 使用新数据库，或原样重放到未修订的演示库。`ingest` 接受 JSON 数组，记录字段见 `fixtures/demo.json` 的 `capture`。可乱序导入，引用不完整的证据会在相应业务视图挂起。每类记录以 `id` 和正整数 `rev` 确定身份。同版本同内容可重放，异内容使整批回滚，最大版本参与现行重算。读取使用同一 SQLite 快照，签发在写事务内核对当前依据。

## 坐标、剪除和签发

所有长度用毫米，卷重用克，金额用整数分。纵横区间左闭右开，扫描矩形以 `[纵起, 纵止, 横起, 横止]` 表示。计数段用 `base + raw - first` 投影到单条纸机线的公共纵轴，每个计数段只覆盖其声明的原始区间。物理母卷相交时扣留冲突范围内的母卷子卷。搭接和断纸也走该公共轴，所以同一搭接可以冲击两卷。

同缺陷多次扫描按矩形并集计算。经核验且覆盖完整补丁的扫描窗才提供缺陷证据。保留纸幅必须被有效扫描窗并集完全覆盖。实剪用相对母卷卷首的纵向区间，覆盖子卷全宽。刀位冲突、无定位、剪越界或剪空、保留段上的搭接、断纸、严重缺陷、可能相关的未定位严重缺陷和未覆盖区域各自给出扣留原因。观察级缺陷单独保留几何证据。

登记复称时把 `plan` 输出中的 `plan` 值带入 `weigh LANE TICKET PLAN REV GRAMS`。同一剪刀方案只有一张有效复称票才可按含端点公差判定。签发命令 `issue LANE BASIS OPERATOR` 使用 `finish` 的 `basis`。签发将证据快照写入 `issue`，按当前依据幂等。影响该子卷的后续证据回执可使旧签发变为 `stale`。修正后需用新依据重签。

## 索赔重算

客户采样坐标可跨母卷。按已核验的客户样带并集分母切分证据；存在无法定位或未核验的样带时全额暂挂。确认缺陷落在唯一当前签发子卷、确切剪除段或其他待决区域，分别进入子卷、剪除和暂挂桶。扫描足以证明样区无已确认目标缺陷时另列该桶。报告附每块区域的来源标签，采用最大余数法分配整数分，同余数按桶 ID 排序。

样例中 A、C 有当前签发，B、D 扣留。`D-cross` 实际面积 400 mm²，跨两卷各 200 mm²。`claim-7` 的 1001 分分到 A 125、C 175、剪除 200、暂挂 501。补录 `correction.json` 后 A、C 签发均失效，`claim-7` 剩余有效剪除证据 25 分，976 分暂挂。`recompute` 从工位原始修订和签发凭证重建这些输出。

## 共刀试排

横切一次落刀同时切断母卷上全部已装刀道。`trial FILE` 读入试排请求：母卷、已装刀道、允许落刀的母卷局部毫米点（至多十二个）、两次落刀的最小间隔、内刀次数上限和至多六张订单，每单指定一道及可收的连续长度。母卷两端与选中的内刀点划出物理纸段，刀距与次数用同一份落刀序列校验；每单占用一道上相邻两刀之间的一个完整纸段，同段只供一单。纸段必须被现行验证扫描完整覆盖，遇拒收缺陷、搭接或断纸不可接单；缺少定位或刀位冲突给出原因。全单可排时输出可照着落刀的加工单：落刀点、每单的道与刀间区间、各道纸段状态。不能全排时给出成员最少的不可同排订单组，并对组内每单给出暂缓该单的可执行试排。查询只读，工位拿到试排单后仍走既有实剪与称重流程。
