from datasets import Dataset, load_dataset, load_from_disk
from ragas import evaluate, EvaluationDataset
from ragas.embeddings import BaseRagasEmbeddings
from ragas.llms import BaseRagasLLM
from ragas.metrics.base import Metric

# Metrics
from ragas.metrics import LLMContextPrecisionWithReference, LLMContextRecall, ContextEntityRecall, NoiseSensitivity, ResponseRelevancy, Faithfulness
from ragas.metrics import FactualCorrectness, SemanticSimilarity

from langchain_huggingface import HuggingFaceEmbeddings
from ..rag.pipeline import RAGPipeline, RAGConfig
from ..index.indexer import IndexerConfig, Indexer, DBConfig
from ..rag.retriever import RetrieverConfig
from ..rag.llm import LLM, LLMConfig
from ..type import MultimodalSample
from ..utils import load_config
from typing import Union, List
from dataclasses import dataclass, field


class RAGASMetrics:
    METRIC_LOOKUP = {
        # Retrieval Augmented Generation Metrics
        "ContextPreLLMContextPrecisionWithReferencecision": LLMContextPrecisionWithReference,
        "LLMContextRecall": LLMContextRecall,
        "ContextEntityRecall": ContextEntityRecall,
        "NoiseSensitivity": NoiseSensitivity,
        "ResponseRelevancy": ResponseRelevancy,
        "Faithfulness": Faithfulness,

        # Natural Language Comparison Metrics
        "FactualCorrectness": FactualCorrectness,
        "SemanticSimilarity": SemanticSimilarity,
 
    }

    @classmethod
    def get_metric_class(cls, metric_name):
        """
        Given a metric name, return the corresponding metric class.
        """
        if metric_name in cls.METRIC_LOOKUP:
            return cls.METRIC_LOOKUP[metric_name]()
        else:
            raise ValueError(f"Metric '{metric_name}' not found in the RAGAS metrics list.")

    @classmethod
    def get_all_metrics(cls):
        """
        Return a list of all available metric classes.
        """
        return [cls.get_metric_class(metric) for metric in cls.METRIC_LOOKUP]
    
    @staticmethod
    def _parse_metrics(metrics: List[str]):
        if not isinstance(metrics, list):
            raise TypeError("The 'metrics' parameter must be a list of metric names (strings).")
        
        parsed_metrics = []
        for metric_name in metrics:
            if isinstance(metric_name, str):
                try:
                    parsed_metrics.append(RAGASMetrics.get_metric_class(metric_name))
                except ValueError:
                    raise ValueError(f"Invalid metric provided: {metric_name}. Metric not found.")
            else:
                raise ValueError(f"Invalid metric provided: {metric_name}. Each metric must be a string.")
        
        return parsed_metrics
@dataclass
class EvalConfig:
    """RAG Eval Configuration"""
    hf_dataset_name: str
    split: str
    hf_feature_map: dict
    metrics: Union[str, List[str]]
    embeddings_name: str
    llm: LLMConfig = field(default_factory=lambda: LLMConfig(llm_name='gpt2'))    
   

class RAGEvaluator:
    dataset: Dataset
    metrics: Metric | List[Metric]
    evaluator_llm: BaseRagasLLM
    embeddings: BaseRagasEmbeddings

    def __init__(self, dataset, metrics, evaluator_llm, embeddings):
        self.dataset = dataset
        self.metrics = metrics
        self.evaluator_llm = evaluator_llm
        self.embeddings = embeddings

    @classmethod
    def from_config(cls, config: str | EvalConfig):
        if isinstance(config, str):
            config = load_config(config, EvalConfig)
        # Load and prepare the dataset
        hf_dataset = load_dataset(config.hf_dataset_name, split=config.split)

        dataset = hf_dataset.rename_columns(config.hf_feature_map)

        # Add 'retrieved_contexts' and 'response' as empty fields
        dataset = dataset.map(lambda x: {"retrieved_contexts": [], "response": []})

        # Parse and store metrics
        metrics = RAGASMetrics._parse_metrics(config.metrics)

        # Define evaluator LLM and embeddings
        evaluator_llm = LLM.from_config(config.llm)
        embeddings = HuggingFaceEmbeddings(model_name=config.embeddings_name)

        return cls(dataset, metrics, evaluator_llm, embeddings)


    def _get_eval_dataset(self, outputs: List[dict]) -> Dataset:
        """
        Update the dataset with 'response' and 'retrieved_contexts'.
        """

        def add_outputs_to_record(record, output):
            record["response"] = output["answer"]
            record["retrieved_contexts"] = [output["context"]]
            return record

        updated_dataset = self.dataset.map(
            lambda record, idx: add_outputs_to_record(record, outputs[idx]),
            with_indices=True
        )

        return updated_dataset

    def __call__(self, indexer_config: IndexerConfig, rag_config: RAGConfig):
        queries = self.dataset["user_input"]
        query_ids = self.dataset["query_ids"]
        rag_outputs = []

        # Indexing logic
        indexer = Indexer.from_config(indexer_config)

        collection_name = indexer.dense_model_name.replace("-", "_") 
        if not indexer.client.has_collection(collection_name):
            for i, documents in enumerate(self.dataset["corpus"]):
                print('Creating the indexer...')
                indexer.index_documents([MultimodalSample(doc, modalities=[]) for doc in documents],
                                        collection_name=collection_name, partition_name=str(query_ids[i]))
                print("Indexer created.")

        # RAG initialization
        rag = RAGPipeline.from_config(rag_config)

        # Generate RAG outputs
        for i, query in enumerate(queries):
            query_id = query_ids[i]
            rag_outputs.append(rag(queries={'input': query, 'collection_name': collection_name, 'partition_name': str(query_id)},
                                   return_dict=True)[0])
   
        # Update the dataset with RAG outputs
        eval_dataset = self._get_eval_dataset(rag_outputs)

        return evaluate(
            dataset=EvaluationDataset.from_hf_dataset(eval_dataset),
            metrics=self.metrics,
            llm=self.evaluator_llm,
            embeddings=self.embeddings,
            batch_size=4,
        ).to_pandas()
