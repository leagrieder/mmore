import os
import sys

from ragas.metrics import LLMContextRecall, Faithfulness, FactualCorrectness, SemanticSimilarity
from langchain_huggingface import HuggingFacePipeline
import argparse
import pandas as pd
from datasets import load_dataset  # Load datasets from the HF Hub
from src.mmore.rag.evaluator import EvalConfig, RAGEvaluator
from src.mmore.rag.llm import LLMConfig, LLM
from src.mmore.index.indexer import DBConfig

from dotenv import load_dotenv
load_dotenv()

MOCK_EVALUATOR_CONFIG = './examples/rag/evaluation/rag_eval_example_config.yaml'
MOCK_INDEXER_CONFIG = './examples/rag/evaluation/indexer_eval_example_config.yaml'
MOCK_RAG_CONFIG = './examples/rag/evaluation/rag_evaluated_example_config.yaml'

def get_args():
    parser = argparse.ArgumentParser(description='Run RAG Evaluation pipeline with specified parameters or use default mock data')
    parser.add_argument('--eval-config', type=str, default=MOCK_EVALUATOR_CONFIG, help='Path to a rag evaluator config file.')
    parser.add_argument('--indexer-config', type=str, default=MOCK_INDEXER_CONFIG, help='Path to an Indexer config file.')
    parser.add_argument('--rag-config', type=str, default=MOCK_RAG_CONFIG, help='Path to a rag config file.')

    return parser.parse_args()

if __name__ == "__main__":
    args = get_args()

    # Instantiate RAGEvaluator
    evaluator = RAGEvaluator.from_config(args.eval_config)

    # Run the evaluation
    result = evaluator(
        indexer_config = args.indexer_config,
        rag_config = args.rag_config
    )

    print(result)