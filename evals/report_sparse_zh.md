# Sparse-zh Experiment Report — open-ant (Phase 5E)

- 生成时间: 2026-08-27T12:55:35+08:00
- 集合: `open_ant_sparse_exp_fastembed` / `open_ant_sparse_exp_jieba`（每次运行重建）
- 语料: 20 篇 / 20 chunks（`evals/dataset_retrieval.py`）
- 查询: 30 条标注 query（ground truth doc 级）
- Dense embedding: sentence-transformers BAAI/bge-small-zh-v1.5 (dim=512)（dim=512）
- Sparse: fastembed Qdrant/bm25 vs jieba lcut（版本 0.42.1）
- 指标口径: recall@5 / MRR / NDCG@10，doc 级去重（`evals/metrics.py`）
- 降级状态: 真实 embedding
- Phase 3D 基线（run_retrieval_eval）: dense recall@5=0.9833 / hybrid(RRF) recall@5=0.9167

## 汇总对照

| sparse_model | 模式 | recall@5 | MRR | NDCG@10 |
|---|---|---|---|---|
| fastembed | dense-only | 0.9833 | 0.9028 | 0.9176 |
| fastembed | hybrid (RRF) | 0.9833 | 0.9028 | 0.9176 |
| jieba | dense-only | 0.9833 | 0.9028 | 0.9176 |
| jieba | hybrid (RRF) | 0.9667 | 0.8261 | 0.8500 |

## 结论

- fastembed hybrid vs jieba hybrid（recall@5）: 0.9833 → 0.9667（Δ -0.0167）
- jieba hybrid vs dense-only: recall@5 Δ -0.0167，MRR Δ -0.0767，NDCG@10 Δ -0.0676
- 判定: jieba 未跑赢 fastembed——需复查索引空间/分词质量（见 _sparse_vectors docstring 的重建注意事项）。

> 说明: 两集合 dense-only 数字一致属预期（同一 dense 向量）；切换 sparse_model 后必须
> 重建集合（delete_by_filter 或 recreate），fastembed 与 jieba 的索引空间不兼容。
> jieba 模式不加载 fastembed ONNX 模型（省内存），且索引空间固定为 1M 的 sha256 哈希。
