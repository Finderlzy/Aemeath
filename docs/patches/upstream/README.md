# 上游 GPT-SoVITS 补丁（不属于 Aemeath 交付代码）

本目录存放**针对上游 GPT-SoVITS 检出**的补丁，用于记录技术验证过程中必需的改动。

> **这些补丁不参与 Aemeath 的构建，也不通过 `docs/patches/README.md` 的 0001–0004 补丁序列
> 应用。** 它们作用于 `E:\WorkSpace\Tools\GPT-SoVITS`（一个独立的上游检出，不入本仓库版本
> 管理），**未被合入上游**。使用前须确认其对目标上游版本仍然适用。

## `s2-train-single-gpu-ddp.patch`

**上游版本**：`48b1a01`
**发现于**：V2-T05 训练接入技术验证（[验收记录第二十一节](../../acceptance.md#二十一v2-t05-训练接入技术验证2026-09-15)）

### 解决的问题

Windows 单卡环境下，SoVITS 训练（`GPT_SoVITS/s2_train.py`）在第一个训练步崩溃，子进程以
Windows 退出码 `3221225477`（`0xC0000005`，访问违例）终止：

```
loaded pretrained .../s2G2333k.pth <All keys matched successfully>
loaded pretrained .../s2D2333k.pth <All keys matched successfully>
start training from epoch 1
torch.multiprocessing.spawn.ProcessExitedException:
process 0 terminated with exit code 3221225477
```

崩溃点位于 `s2_train.py` 的 `scaler.scale(loss_disc_all).backward()`。数据集与预训练权重都已
正常加载，**且该错误无法在 Python 层捕获**，因此没有可用的 Python 栈。

### 根因

`main()` 用 `n_gpus = torch.cuda.device_count()` 取卡数，随后**无条件**执行
`mp.spawn` + `dist.init_process_group`（Gloo）+ `DistributedDataParallel`，从不判断
`n_gpus > 1`。`gpu_numbers` 配置只影响 `CUDA_VISIBLE_DEVICES`，无法绕过该路径——
即使设备数为 1，仍然走 DDP 并在 `backward()` 崩溃。

**这是上游仍开放的缺陷**：[RVC-Boss/GPT-SoVITS#2806](https://github.com/RVC-Boss/GPT-SoVITS/issues/2806)
记录了完全相同的现象与退出码。上游另提供的 `s2_train_v3_lora.py` 已带 `use_ddp = n_gpus > 1`
判断，但其数据加载器硬编码 V3/V4，**不能用于 v2**。

### 补丁内容

1. 引入 `use_ddp = n_gpus > 1`，用它守卫 `dist.init_process_group`、`DistributedBucketSampler`
   与两处 `DDP(...)` 包装。
2. 非 DDP 路径下把模型真正搬到 GPU。原代码在模块层把 `device` 硬编码为 `"cpu"`，并依赖 DDP 的
   `device_ids` 完成放置；不走 DDP 时会报
   `RuntimeError: Expected all tensors to be on the same device`。
3. 把 `net_g.module` / `net_d.module` / `generator.module` 的判断从 `torch.cuda.is_available()`
   改为 `use_ddp`，并在非 DDP 时跳过 `batch_sampler.set_epoch`。

### 验证结果

应用后同一份训练配置**训练成功**：2 epoch、119.35s、峰值显存 6087MB，产出
`aemeath_verify_v2_e2_s200.pth`（81.07MB），产物经真实合成与 SenseVoice 回转确认可听可识别。

### 应用方式

```powershell
cd E:\WorkSpace\Tools\GPT-SoVITS
git apply E:\WorkSpace\Aemeath\docs\patches\upstream\s2-train-single-gpu-ddp.patch
```

补丁基准为该检出的 `48b1a01`。**应用前建议先 `git apply --check` 确认可套用**；
若目标检出已含上游修复，补丁会因上下文不符而失败——此时应改用上游版本，不要强行套用。

> 本次生成补丁时发现：直接把 `git diff` 输出重定向到文件会受 PowerShell 文本编码影响，
> 产物可能**无法套用**。上面保存的补丁已用 `git apply --check` 对干净文件实测通过
> （退出码 0），并在套用后重新跑通训练。后续更新该补丁时同样需要实测验证可套用性。

### 状态说明

- 该补丁**未提交给上游**，也**不在本仓库版本管理范围内生效**。
- V2-T06（完整声音训练向导）若要真正训练，必须先满足此前提（应用本补丁，或升级到上游已修复
  的版本）。
- 上游修复正式发布后应改用上游版本，并重新验证本文件所列读数。
