#!/usr/bin/env python3
import click
import json
from pathlib import Path
from repo_viz import ArchitectureAnalyzer
from dataclasses import asdict

@click.group()
def cli():
    """Generate architecture diagrams from Python repositories"""
    pass

@cli.command()
@click.argument('target')
@click.option('--output', '-o', help='Output file (mermaid or json)')
@click.option('--format', '-f', type=click.Choice(['mermaid', 'json']), default='mermaid')
def analyze(target, output, format):
    """Analyze a Git repo or local directory"""
    analyzer = ArchitectureAnalyzer(
        repo_url=target if target.startswith(('http://', 'https://')) else None,
        local_path=target if not target.startswith(('http://', 'https://')) else None
    )
    
    if format == 'mermaid':
        result = analyzer.generate_mermaid()
    else:
        result = json.dumps({
            'components': {name: asdict(comp) for name, comp in analyzer.components.items()}
        }, indent=2, default=str)

    if output:
        Path(output).write_text(result)
        click.echo(f"Output written to {output}")
    else:
        click.echo(result)

if __name__ == '__main__':
    cli()