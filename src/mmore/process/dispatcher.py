import logging
from typing import Any, Dict, List, Type, Tuple, Optional
from .processors.url_processor import URLProcessor
from .crawler import DispatcherReadyResult, FileDescriptor
from .processors.processor import AutoProcessor, Processor, ProcessorRegistry, ProcessorConfig
import torch
import logging
import os
from operator import itemgetter
from tqdm import tqdm
from dask.distributed import as_completed, Client
import dask.config
from src.mmore.type import MultimodalSample

logger = logging.getLogger(__name__)


class ComputeDescriptor:
    @staticmethod
    def get_desc() -> None:
        if torch.cuda.is_available():
            num_gpus = torch.cuda.device_count()
            if num_gpus > 0:
                gpu_size = torch.cuda.get_device_properties(0).total_memory
                # All GPUs are assumed to have the same size
                logging.info(
                    f"Detected {num_gpus} GPUs with {gpu_size} bytes of memory."
                )
        else:
            num_gpus = 0
            gpu_size = None

        return {
            "num_gpus": num_gpus,
            "gpu_size": gpu_size,
        }


class DispatcherConfig:
    """
    A configuration class for the dispatcher.
    
    Save the results to the output path.
    Following sturcture is used:
    
    output_path
    ├── processors
    |   ├── Processor_type_1
    |   |   └── results.jsonl
    |   ├── Processor_type_2
    |   |   └── results.jsonl
    |   ├── ...
    |   
    └── merged
        └── merged_results.jsonl
            
    """

    def __init__(
            self,
            use_fast_processors: bool = True,
            distributed: bool = False,
            scheduler_file: Optional[str] = None,
            output_path: Optional[str] = None,
            processor_config: Optional[Dict] = None,
            process_batch_sizes: Optional[Dict] = None,
            batch_multiplier: int = 1,
    ) -> None:
        """
        Initialize the DispatcherConfig object.

        :param use_fast_processors: Whether to use fast processors.
        :param distributed: Whether the dispatcher is running in distributed mode.
        :param scheduler_file: Path to the scheduler file (if distributed).
        :param output_path: Path to save the output. If None, the output is not saved.
        :param batch_multiplier: Multiplier for batch sizes.
        """
        self.use_fast_processors = use_fast_processors
        self.distributed = distributed
        self.scheduler_file = scheduler_file
        self.output_path = output_path
        self.processor_config = processor_config
        self.process_batch_sizes = process_batch_sizes
        self.batch_multiplier = batch_multiplier

    from typing import Dict, Optional

    @staticmethod
    def from_dict(config: Dict) -> "DispatcherConfig":
        """Create a DispatcherConfig object from a dictionary."""
        return DispatcherConfig(
            use_fast_processors=config.get("use_fast_processors", True),
            distributed=config.get("distributed", False),
            scheduler_file=config.get("scheduler_file"),
            output_path=config.get("output_path"),
            processor_config=config.get("processor"),
            process_batch_sizes=config.get("process_batch_sizes"),
            batch_multiplier=config.get("batch_multiplier", 1),
        )

    @staticmethod
    def from_yaml(yaml_path: str):
        import yaml
        try:
            with open(yaml_path, "r") as file:
                config = yaml.safe_load(file)
            return DispatcherConfig.from_dict(config)
        except (FileNotFoundError, yaml.YAMLError):
            logger.error(f"[Dispatcher] Error processing file {yaml_path}")
            raise

    def to_dict(self) -> Dict:
        """Convert the DispatcherConfig object to a dictionary."""
        return {
            "use_fast_processors": self.use_fast_processors,
            "distributed": self.distributed,
            "scheduler_file": self.scheduler_file,
            "output_path": self.output_path,
            "processor": self.processor_config,
            "process_batch_sizes": self.process_batch_sizes,
            "batch_multiplier": self.batch_multiplier,
        }

    def __str__(self) -> str:
        """Return a string representation of the DispatcherConfig object."""
        return (
            f"DispatcherConfig("
            f"use_fast_processors={self.use_fast_processors}, "
            f"distributed={self.distributed}, "
            f"scheduler_file={self.scheduler_file}, "
            f"output_path={self.output_path}, "
            f"processor_config={self.processor_config}, "
            f"process_batch_sizes={self.process_batch_sizes}, "
            f"batch_multiplier={self.batch_multiplier}"
            f")"
        )


class Dispatcher:
    """
    Takes a converted crawl result and dispatches it to the appropriate processor.
    """

    def __init__(
            self,
            result: DispatcherReadyResult,
            config: DispatcherConfig = DispatcherConfig(),
            start_cluster=False,
    ):
        self.result = result
        self.config = config
        self.start_cluster = start_cluster
        self.intermediate_map = {}

    def _bucket_files(self) -> None:
        """
        Categorize files and URLs into the appropriate processors.
        """

        processor_map = {
            processor: [] for processor in ProcessorRegistry.get_processors()
        }

        for file_path_list in self.result.file_paths.values():
            for file in file_path_list:
                processor = AutoProcessor.from_file(file)
                logger.debug(f"Assigned file {file.file_path} to processor: {processor}")
                processor_map[processor].append(file)

        url_processor = URLProcessor
        processor_map[url_processor].extend(self.result.urls)

        self.intermediate_map = processor_map

    def _dispatch_local(
            self, task_lists: List[Tuple[Type[Processor], List[FileDescriptor]]]
    ) -> Any:
        """
        Dispatches the tasks locally.
        """
        processor_configs = self.config.processor_config or {}

        for processor, files in task_lists:
            processor_config = processor_configs.get(processor.__name__, [])
            processor_config = {list(d.keys())[0]: list(d.values())[0] for d in processor_config}
            processor_config['output_path'] = self.config.output_path

            logger.info(
                f"Dispatching locally {len(files)} files with ({sum([processor.get_file_len(file) for file in files])}) pages to {processor.__name__}"
            )
            processor_config = ProcessorConfig(custom_config=processor_config)
            proc = processor(processor_config)
            res = proc(files, self.config.use_fast_processors)
            self.save_individual_processor_results(res, processor.__name__)
            yield res

    def dispatch(self) -> List[List[MultimodalSample]]:
        """
        Dispatches the result to the appropriate processor.
        """

        def batch_list(
                lst: List, obj_batch_size: int, processor: Type[Processor]
        ) -> List[List]:
            """
            Creates optimized batches using best-fit decreasing algorithm.

            Args:
                lst: List of objects to batch
                obj_batch_size: Maximum allowed batch size
                processor: Processor that can determine object sizes

            Returns:
                List of batched objects optimized for size
            """
            # Create (object, size) tuples and sort by size descending
            items = [(obj, processor.get_file_len(obj)) for obj in lst]
            items = [item for item in items if item[1] != -1]

            items.sort(key=itemgetter(1), reverse=True)

            batches = [[]]  # List of object lists
            batch_sizes = [0]  # Parallel array tracking batch sizes

            for obj, size in items:
                best_fit_idx = -1
                min_remaining = obj_batch_size

                # Find best fitting-batch
                for i, batch_size in enumerate(batch_sizes):
                    remaining = obj_batch_size - (batch_size + size)
                    if 0 <= remaining < min_remaining:
                        min_remaining = remaining
                        best_fit_idx = i

                if best_fit_idx >= 0:
                    batches[best_fit_idx].append(obj)
                    batch_sizes[best_fit_idx] += size
                else:
                    batches.append([obj])
                    batch_sizes.append(size)

            return batches

        self._bucket_files()

        batch_sizes = self.config.process_batch_sizes or {}
        batch_sizes = {list(d.keys())[0]: int(list(d.values())[0]) for d in batch_sizes}

        task_lists = []
        for processor, file_list in self.intermediate_map.items():
            if len(file_list) > 0:
                batched_files = batch_list(
                    file_list,
                    self.config.batch_multiplier * batch_sizes.get(processor.__name__, 100),
                    processor,
                )
                task_lists.extend([(processor, batch) for batch in batched_files])
        results = []
        if self.config.distributed:
            results = self._dispatch_distributed(task_lists)
        else:
            results = self._dispatch_local(task_lists)

        return results

    def __call__(self) -> List[List[MultimodalSample]]:
        return self.dispatch()

    def save_individual_processor_results(self, results: List[MultimodalSample], cls_name) -> None:
        if not self.config.output_path:
            return
        
        processor_output_path = os.path.join(self.config.output_path, "processors", cls_name)
        os.makedirs(processor_output_path, exist_ok=True)
        output_file = os.path.join(processor_output_path, "results.jsonl")
        MultimodalSample.to_jsonl(output_file, results)

        logger.info(f"Results saved to {output_file}")
