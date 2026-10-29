"""Read-only proposal preflight against a dispatcher-owned job context."""
import argparse
import json
from consumer_return import review, read_json
from receipt_status import load_population


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--context', required=True)
    args = parser.parse_args()
    try:
        context = read_json(args.context)
        if not isinstance(context, dict) or set(context) != {'output_path', 'receipt_paths', 'stores'}:
            raise ValueError('explicit job context required')
        accepted, rejected = review(context['output_path'], context['receipt_paths'], context['stores'])
        try:
            population = load_population(context['receipt_paths'])
        except (OSError, ValueError, TypeError, KeyError):
            population = {'status': 'unknown', 'semantic_accuracy': 'unknown', 'learning_outcome': 'unverified'}
        print(json.dumps({'proposal_validation': 'partial' if rejected else 'valid',
                          'valid_indexes': [row['index'] for row in accepted], 'rejected': rejected,
                          'receipt_population': population,
                          'document_writes': 0, 'pending_writes': 0, 'learning_outcome': 'unverified'}))
        return 2 if rejected else 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'proposal_validation': 'unknown', 'error': type(exc).__name__,
                          'document_writes': 0, 'pending_writes': 0, 'learning_outcome': 'unverified'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
