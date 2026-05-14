import torch
import warnings


# sm_120 = Blackwell (RTX 50x). PyTorch hỗ trợ stable từ 2.7.0 + CUDA 12.8.
_MIN_SUPPORTED_SM_FOR_TORCH = {
    (12, 0): "2.7.0",  # Blackwell consumer (RTX 5090/5080/5070)
    (10, 0): "2.4.0",  # Hopper
    (9, 0):  "2.0.0",  # Ada
}


def _check_blackwell_compatibility(device_idx: int = 0) -> None:
    """Log compute capability và cảnh báo nếu torch chưa hỗ trợ kernel cho sm hiện tại.

    Triệu chứng khi không tương thích: chạy được `torch.cuda.is_available()` nhưng
    `model(...)` raise `CUDA error: no kernel image is available for execution on the
    device`. Hàm này log chẩn đoán sớm để dễ debug.
    """
    from utils.logger import logger

    try:
        major, minor = torch.cuda.get_device_capability(device_idx)
        gpu_name = torch.cuda.get_device_name(device_idx)
        torch_ver = torch.__version__
        cuda_ver = torch.version.cuda

        logger.info(
            f"CUDA device[{device_idx}]: {gpu_name} "
            f"(sm_{major}{minor}), torch={torch_ver}, cuda={cuda_ver}"
        )

        supported = torch.cuda.get_arch_list()  # ['sm_50', 'sm_60', ..., 'sm_120']
        current_sm = f"sm_{major}{minor}"
        if supported and current_sm not in supported and f"sm_{major}0" not in supported:
            min_ver = _MIN_SUPPORTED_SM_FOR_TORCH.get((major, minor), "?")
            warnings.warn(
                f"torch {torch_ver} compiled for {supported} nhưng GPU là {current_sm}. "
                f"Cần torch>={min_ver} + CUDA 12.8 (cu128) cho Blackwell. "
                f"Cài: pip install --upgrade torch torchvision torchaudio "
                f"--index-url https://download.pytorch.org/whl/cu128",
                RuntimeWarning,
                stacklevel=2,
            )
            logger.error(
                f"GPU {current_sm} không có trong arch_list={supported}. "
                f"Inference sẽ raise 'no kernel image available'."
            )
    except Exception as e:
        # Không để chẩn đoán phá flow init
        logger.warning(f"Bỏ qua kiểm tra compute capability: {e}")


def initialize_device():
    if torch.cuda.is_available():
        _check_blackwell_compatibility()
        return torch.device('cuda')
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device('mps')
    else:
        return torch.device('cpu')
