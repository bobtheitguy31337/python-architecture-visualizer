from dataclasses import dataclass, field
from enum import Enum
import ast
import os
from pathlib import Path
from typing import Dict, Set, List, Optional
import git
import re
import docker
from coverage import Coverage
from radon.metrics import mi_visit, mi_rank
from radon.complexity import cc_visit
from bandit.core import manager as bandit_manager
import tempfile

@dataclass
class SecurityIssue:
    severity: str
    issue_type: str
    filename: str
    line: int
    description: str

@dataclass
class PerformanceMetric:
    complexity: int
    maintainability: float
    io_operations: int
    db_operations: int
    network_calls: int

@dataclass
class DockerLayer:
    command: str
    size: int
    dependencies: Set[str]

@dataclass
class Component:
    name: str
    type: str  # Changed from ComponentType enum for simplicity
    path: Path
    performance: PerformanceMetric = None
    security_issues: List[SecurityIssue] = field(default_factory=list)
    test_coverage: float = 0.0
    docker_layers: List[DockerLayer] = field(default_factory=list)
    dependencies: Set[str] = field(default_factory=set)
    api_endpoints: Set[str] = field(default_factory=set)
    external_urls: Set[str] = field(default_factory=set)

class ArchitectureAnalyzer:  # Renamed from AdvancedAnalyzer
    def __init__(self, repo_url: Optional[str] = None, local_path: Optional[str] = None):
        if repo_url:
            # Clone to temp directory
            self.temp_dir = tempfile.mkdtemp()
            git.Repo.clone_from(repo_url, self.temp_dir)
            self.repo_path = Path(self.temp_dir)
        elif local_path:
            self.repo_path = Path(local_path)
            self.temp_dir = None
        else:
            raise ValueError("Either repo_url or local_path must be provided")
            
        self.components: Dict[str, Component] = {}
        try:
            self.docker_client = docker.from_env()
        except:
            self.docker_client = None

    def _detect_component_type(self, file_path: Path) -> str:
        """Detect component type based on content and imports."""
        content = file_path.read_text().lower()
        name = file_path.stem.lower()
        
        if "test" in name:
            return "test"
        elif any(x in content for x in ["fastapi", "flask", "django"]):
            return "api"
        elif "model" in name:
            return "model"
        return "module"

    def _extract_dependencies(self, file_path: Path) -> Set[str]:
        """Extract Python module dependencies from imports."""
        with open(file_path) as f:
            tree = ast.parse(f.read())
        
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    deps.add(name.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    deps.add(node.module.split('.')[0])
        return deps

    def analyze_performance(self, file_path: Path) -> PerformanceMetric:
        with open(file_path) as f:
            content = f.read()
            
        # Cyclomatic complexity
        complexity = sum(cc.complexity for cc in cc_visit(content))
        
        # Maintainability index
        maintainability = mi_visit(content, multi=True)
        
        # Count IO/DB/Network operations
        tree = ast.parse(content)
        io_ops = len([n for n in ast.walk(tree) if isinstance(n, ast.Call) 
                     and any(io_name in getattr(n.func, 'id', '') 
                     for io_name in ['open', 'read', 'write'])])
                     
        db_ops = len([n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and any(db_op in getattr(n.func, 'attr', '')
                     for db_op in ['execute', 'query', 'commit'])])
                     
        network_calls = len([n for n in ast.walk(tree) if isinstance(n, ast.Call)
                           and any(net_op in getattr(n.func, 'attr', '')
                           for net_op in ['get', 'post', 'request'])])
                           
        return PerformanceMetric(
            complexity=complexity,
            maintainability=maintainability,
            io_operations=io_ops,
            db_operations=db_ops,
            network_calls=network_calls
        )

    def analyze_security(self, file_path: Path) -> List[SecurityIssue]:
        # Initialize Bandit with default config
        from bandit.core import config as b_config
        conf = b_config.BanditConfig()
        b_mgr = bandit_manager.BanditManager(conf, 'file')
        
        # Run analysis
        b_mgr.discover_files([str(file_path)])
        b_mgr.run_tests()
        
        issues = []
        for issue in b_mgr.get_issue_list():
            issues.append(SecurityIssue(
                severity=str(issue.severity),
                issue_type=str(issue.test_id),
                filename=str(issue.fname),
                line=int(issue.lineno),
                description=str(issue.text)
            ))
        
        return issues

    def analyze_test_coverage(self) -> Dict[str, float]:
        cov = Coverage()
        cov.start()
        
        # Run tests if pytest is available
        try:
            import pytest
            pytest.main(['--rootdir', str(self.repo_path)])
        except ImportError:
            pass
            
        cov.stop()
        cov.save()
        
        # Process coverage data
        data = {}
        for filename in cov.get_data().measured_files():
            rel_path = os.path.relpath(filename, start=str(self.repo_path))
            analysis = cov.analysis2(filename)
            total_lines = len(analysis[1]) + len(analysis[2])  # covered + missing
            covered_lines = len(analysis[1])
            coverage_pct = (covered_lines / total_lines * 100) if total_lines > 0 else 0
            data[rel_path] = coverage_pct
            
        return data

    def analyze_docker(self) -> List[DockerLayer]:
        dockerfile = self.repo_path / 'Dockerfile'
        if not dockerfile.exists():
            return []
            
        layers = []
        image = None
        
        try:
            # Build image to analyze layers
            image, _ = self.docker_client.images.build(
                path=str(self.repo_path),
                rm=True
            )
            
            # Analyze each layer
            for layer in image.history():
                layers.append(DockerLayer(
                    command=layer['CreatedBy'],
                    size=layer['Size'],
                    dependencies=self._extract_docker_deps(layer['CreatedBy'])
                ))
                
        finally:
            if image:
                self.docker_client.images.remove(image.id, force=True)
                
        return layers

    def _extract_docker_deps(self, command: str) -> Set[str]:
        deps = set()
        
        # Extract pip requirements
        if 'pip install' in command:
            deps.update(re.findall(r'pip install\s+([\w\-=<>\.]+)', command))
            
        # Extract apt packages
        if 'apt-get install' in command:
            deps.update(re.findall(r'apt-get install\s+([\w\-]+)', command))
            
        return deps

    def analyze(self):
        """Main analysis entry point"""
        for file in self.repo_path.rglob("*.py"):
            # Skip test files for now
            if "test" in file.stem.lower():
                continue
                
            component = Component(
                name=file.stem,
                type=self._detect_component_type(file),
                path=file,
                dependencies=self._extract_dependencies(file)
            )
            
            # Run analyzers
            component.performance = self.analyze_performance(file)
            component.security_issues = self.analyze_security(file)
            
            self.components[file.stem] = component

    def generate_mermaid(self) -> str:
        if not self.components:
            self.analyze()
                
        mermaid = ["graph TD", "    %% Style definitions"]
        
        # Add style classes with CC thresholds
        styles = {
            "high": "fill:#f44336,color:white", # Red for CC > 50
            "medium": "fill:#fb8c00,color:white", # Orange for CC 20-50
            "low": "fill:#81c784,color:black"  # Green for CC < 20
        }
        
        for name, style in styles.items():
            mermaid.append(f"    classDef {name} {style}")

        # Group by layers
        layers = {
            "Client": [c for c in self.components.values() if "client" in c.name.lower()],
            "API": [c for c in self.components.values() if "api" in c.name.lower()],
            "Core": [c for c in self.components.values() if any(x in c.name.lower() for x in ["core", "type", "base"])]
        }
        
        # Add subgraphs and nodes
        for layer, comps in layers.items():
            if comps:
                mermaid.extend([f"    subgraph {layer} Layer"])
                for comp in comps:
                    node_id = re.sub(r'\W+', '_', comp.name)
                    style = ":::high" if comp.performance.complexity > 50 else \
                            ":::medium" if comp.performance.complexity > 20 else ":::low"
                    mermaid.append(f"        {node_id}[{comp.name}<br/>CC:{comp.performance.complexity}]{style}")
                mermaid.append("    end\n")

        # Add relationships
        for name, comp in self.components.items():
            node_id = re.sub(r'\W+', '_', name)
            for dep in comp.dependencies:
                if dep in self.components:
                    dep_id = re.sub(r'\W+', '_', dep)
                    mermaid.append(f"    {node_id} --> {dep_id}")
                    
        return "\n".join(mermaid)

    def __del__(self):
        if self.temp_dir:
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)

if __name__ == "__main__":
    import sys
    analyzer = ArchitectureAnalyzer(local_path=sys.argv[1])
    print(analyzer.analyze())