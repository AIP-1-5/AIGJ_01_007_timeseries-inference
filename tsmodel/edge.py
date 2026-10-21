"""과제 7: 엣지 최적화 — ONNX 변환, 실행 방식별 지연·차이·자원. specs/007-edge.

모델 3종(예측 GRU, 오토인코더, 환기 분류 MLP) × 실행 방식(PyTorch CPU/GPU, ONNX Runtime CPU, TensorRT FP16) × 배치 1·4.
되지 않는 실행 방식은 오류를 그대로 기록한다. 측정 중 tegrastats로 전력·GPU·메모리를 남긴다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

import numpy as np
import torch

MODEL_DIR = os.path.expanduser("~/data/tsinfer/models")
WARMUP, RUNS = 20, 200


def _load_models(sample: dict) -> dict:
    from tsmodel import anomaly, forecast, state

    f, _ = forecast.load(os.path.join(MODEL_DIR, "forecast_gru_schedule.pt"))
    ck = torch.load(os.path.join(MODEL_DIR, "anomaly_ae.pt"), weights_only=False)
    ae = anomaly.AutoEncoder()
    ae.load_state_dict(ck["state_dict"])
    ck = torch.load(os.path.join(MODEL_DIR, "state_vent.pt"), weights_only=False)
    mlp = state.MLP(ck["meta"]["n_in"])
    mlp.load_state_dict(ck["state_dict"])
    return {"forecast": (f.eval(), ["seq", "static"]), "anomaly": (ae.eval(), ["x"]), "vent": (mlp.eval(), ["x"])}


def _export(name, model, names, inputs) -> str:
    path = os.path.join(MODEL_DIR, f"{name}.onnx")
    torch.onnx.export(model, tuple(torch.from_numpy(x[:1]) for x in inputs), path, input_names=names,
                      output_names=["out"], dynamic_axes={n: {0: "batch"} for n in names + ["out"]}, opset_version=17,
                      dynamo=False)
    return path


def _time(fn) -> dict:
    for _ in range(WARMUP):
        fn()
    lat = []
    for _ in range(RUNS):
        s = time.perf_counter()
        fn()
        lat.append((time.perf_counter() - s) * 1000)
    lat = np.array(lat)
    return {"mean_ms": float(lat.mean()), "p95_ms": float(np.percentile(lat, 95))}


def _trt_engine(onnx_path: str, shapes: dict):
    import tensorrt as trt
    logger = trt.Logger(trt.Logger.WARNING)
    builder = trt.Builder(logger)
    net = builder.create_network(0)
    parser = trt.OnnxParser(net, logger)
    with open(onnx_path, "rb") as f:
        if not parser.parse(f.read()):
            raise RuntimeError(str(parser.get_error(0)))
    conf = builder.create_builder_config()
    conf.set_flag(trt.BuilderFlag.FP16)
    prof = builder.create_optimization_profile()
    for n, shp in shapes.items():
        prof.set_shape(n, (1,) + shp, (4,) + shp, (4,) + shp)
    conf.add_optimization_profile(prof)
    plan = builder.build_serialized_network(net, conf)
    if plan is None:
        raise RuntimeError("엔진 빌드 실패")
    return trt.Runtime(logger).deserialize_cuda_engine(plan)


def _trt_runner(engine, names, inputs):
    """torch CUDA 텐서를 버퍼로 쓰는 실행기."""
    ctx = engine.create_execution_context()
    bufs = [torch.from_numpy(x).cuda() for x in inputs]
    for n, b in zip(names, bufs):
        ctx.set_input_shape(n, tuple(b.shape))
        ctx.set_tensor_address(n, b.data_ptr())
    out = torch.empty(tuple(ctx.get_tensor_shape("out")), device="cuda")
    ctx.set_tensor_address("out", out.data_ptr())
    stream = torch.cuda.current_stream().cuda_stream

    def run():
        ctx.execute_async_v3(stream)
        torch.cuda.synchronize()
        return out.cpu().numpy()
    return run


def benchmark(samples: dict, scale: dict, out_dir: str) -> dict:
    """samples: 모델 이름 → 입력 목록(numpy, 앞쪽 N개). scale: 예측 CO₂ 변화량 → ppm 환산(std)."""
    models = _load_models(samples)
    os.makedirs(out_dir, exist_ok=True)
    tlog = os.path.join(out_dir, "tegrastats.log")
    tproc = subprocess.Popen(["tegrastats", "--interval", "500", "--logfile", tlog])
    rows, diffs = [], {}
    try:
        for name, (model, names) in models.items():
            inputs = samples[name]
            onnx_path = _export(name, model, names, inputs)
            with torch.no_grad():
                ref = model(*[torch.from_numpy(x) for x in inputs]).numpy()
            outs = {}
            for batch in (1, 4):
                xb = [x[:batch] for x in inputs]
                tb = [torch.from_numpy(x) for x in xb]
                with torch.no_grad():
                    rows.append({"model": name, "backend": "pytorch_cpu", "batch": batch, **_time(lambda: model(*tb))})
                try:
                    mg = model.cuda()
                    gb = [t.cuda() for t in tb]
                    with torch.no_grad():
                        def g():
                            mg(*gb)
                            torch.cuda.synchronize()
                        rows.append({"model": name, "backend": "pytorch_gpu", "batch": batch, **_time(g)})
                        outs.setdefault("pytorch_gpu", mg(*[torch.from_numpy(x).cuda() for x in inputs]).cpu().numpy())
                    model.cpu()
                except Exception as e:                                # noqa: BLE001
                    model.cpu()
                    rows.append({"model": name, "backend": "pytorch_gpu", "batch": batch, "error": repr(e)[:160]})
                import onnxruntime as ort
                sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
                feed = dict(zip(names, xb))
                rows.append({"model": name, "backend": "onnxruntime_cpu", "batch": batch,
                             **_time(lambda: sess.run(None, feed))})
                outs.setdefault("onnxruntime_cpu", sess.run(None, dict(zip(names, inputs)))[0])
                try:
                    eng = _trt_engine(onnx_path, {n: x.shape[1:] for n, x in zip(names, inputs)})
                    runner = _trt_runner(eng, names, xb)
                    rows.append({"model": name, "backend": "tensorrt_fp16", "batch": batch, **_time(runner)})
                    if batch == 4:
                        res = [ _trt_runner(eng, names, [x[i:i + 4] for x in inputs])()
                                for i in range(0, len(inputs[0]) - len(inputs[0]) % 4, 4)]
                        outs["tensorrt_fp16"] = np.concatenate(res)
                except Exception as e:                                # noqa: BLE001
                    rows.append({"model": name, "backend": "tensorrt_fp16", "batch": batch, "error": repr(e)[:160]})
            d = {}
            for be, o in outs.items():
                r = ref[:len(o)]
                if name == "forecast":
                    co2 = (o[:, :12] - r[:, :12]) * scale["forecast"]          # 정규화 변화량 → ppm
                    d[be] = {"max_abs_co2_ppm": float(np.abs(co2).max()), "mean_abs_co2_ppm": float(np.abs(co2).mean())}
                elif name == "vent":
                    thr = scale["vent_logit"]
                    d[be] = {"flip_rate": float(((o > thr) != (r > thr)).mean()), "n": int(len(o))}
                else:
                    d[be] = {"max_abs_diff": float(np.abs(o - r).max())}
            diffs[name] = d
    finally:
        tproc.terminate()
        tproc.wait()
    res = {"rows": rows, "diffs": diffs, "tegrastats": parse_tegrastats(tlog), "warmup": WARMUP, "runs": RUNS}
    with open(os.path.join(out_dir, "edge_results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    return res


def parse_tegrastats(path: str) -> dict:
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return {}
    ram = [int(m.group(1)) for ln in lines if (m := re.search(r"RAM (\d+)/", ln))]
    gpu = [int(m.group(1)) for ln in lines if (m := re.search(r"GR3D_FREQ (\d+)%", ln))]
    pw = [int(m.group(1)) for ln in lines if (m := re.search(r"VDD_IN (\d+)mW", ln))]
    f = lambda v: {"mean": float(np.mean(v)), "max": float(np.max(v))} if v else None  # noqa: E731
    return {"samples": len(lines), "ram_mb": f(ram), "gpu_util_pct": f(gpu), "power_vdd_in_mw": f(pw)}
