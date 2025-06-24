"""
LLM provider interface for AI-powered summarization
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ContextData:
    """Context data for AI summarization"""
    primary_content: str
    symbol_name: str
    symbol_kind: str
    context_level: str
    source: str
    surrounding_code: Optional[str] = None
    class_context: Optional[str] = None
    function_signature: Optional[str] = None
    related_headers: Optional[list] = None
    imports: Optional[list] = None


@dataclass
class SummaryRequest:
    """Request for AI summarization"""
    content: str
    symbol_name: str
    symbol_kind: str  # "function", "class", "variable", etc.
    file_path: str
    max_tokens: int = 150
    context_level: str = "minimal"  # "minimal", "local", "full"


@dataclass 
class SummaryResponse:
    """AI summarization response"""
    summary: str
    tokens_used: int
    cached: bool = False
    provider: str = "unknown"
    context_level: str = "minimal"


@dataclass
class CallAnalysisRequest:
    """Request for AI call hierarchy analysis"""
    target_function: str
    target_file: str
    target_line: int
    target_column: int
    calls: list  # List of call hierarchy results from clangd
    analysis_level: str = "summary"  # "summary" or "detailed"
    analysis_type: str = "incoming"  # "incoming" or "outgoing"
    

@dataclass
class CallPattern:
    """Represents a programming pattern found in call analysis"""
    pattern_type: str  # "validation", "error_handling", "initialization", etc.
    calls: list  # Calls that match this pattern
    description: str
    confidence: float


@dataclass
class CallAnalysisResponse:
    """AI call hierarchy analysis response"""
    analysis_summary: str
    patterns: List[CallPattern]
    architectural_insights: str
    data_flow_analysis: str
    performance_notes: str
    tokens_used: int
    cached: bool = False
    provider: str = "unknown"


@dataclass
class InheritanceAnalysisRequest:
    """Request for AI inheritance hierarchy analysis"""
    target_type: str
    target_file: str
    target_line: int
    target_column: int
    types: list  # Related types (supertypes or subtypes)
    analysis_level: str = "summary"
    analysis_type: str = "supertypes"  # "supertypes" or "subtypes"


@dataclass
class InheritancePattern:
    """Inheritance pattern identified by AI"""
    pattern_name: str
    types: list  # Types that match this pattern
    description: str
    confidence: float


@dataclass
class InheritanceAnalysisResponse:
    """AI inheritance hierarchy analysis response"""
    analysis_summary: str
    patterns: List[InheritancePattern]
    architectural_insights: str
    design_patterns: str
    refactoring_suggestions: str
    tokens_used: int
    cached: bool = False
    provider: str = "unknown"


class LLMProvider(ABC):
    """Abstract base class for LLM providers"""
    
    @abstractmethod
    async def summarize(self, request: SummaryRequest) -> Optional[SummaryResponse]:
        """Generate a summary for the given content
        
        Args:
            request: SummaryRequest with content and metadata
            
        Returns:
            SummaryResponse with summary and metadata, or None if summarization failed
        """
        pass
        
    @abstractmethod
    async def summarize_with_context(self, context_data: ContextData) -> Optional[SummaryResponse]:
        """Generate a summary using rich context data
        
        Args:
            context_data: ContextData with comprehensive information
            
        Returns:
            SummaryResponse with summary and metadata, or None if summarization failed
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is properly configured
        
        Returns:
            True if provider can be used, False otherwise
        """
        pass
    
    @abstractmethod
    async def analyze_call_hierarchy(self, request: CallAnalysisRequest, 
                                   context_data: Optional[Dict] = None) -> Optional[CallAnalysisResponse]:
        """Analyze call hierarchy with AI insights
        
        Args:
            request: CallAnalysisRequest with call hierarchy data
            context_data: Optional additional context from files
            
        Returns:
            CallAnalysisResponse with AI insights, or None if analysis failed
        """
        pass
    
    @abstractmethod
    async def analyze_inheritance(self, request: InheritanceAnalysisRequest,
                                context_data: Optional[Dict] = None) -> Optional[InheritanceAnalysisResponse]:
        """Analyze inheritance hierarchy with AI insights
        
        Args:
            request: InheritanceAnalysisRequest with inheritance hierarchy data
            context_data: Optional additional context from files
            
        Returns:
            InheritanceAnalysisResponse with AI insights, or None if analysis failed
        """
        pass
        
    def should_summarize(self, context_data: ContextData) -> bool:
        """Decide if content should be summarized based on context
        
        Args:
            context_data: ContextData to evaluate
            
        Returns:
            True if content should be summarized, False otherwise
        """
        # Skip if no substantial content
        if len(context_data.primary_content.strip()) < 100:
            return False
        
        # Smart triggers based on symbol type
        if context_data.symbol_kind in ["getter", "setter", "destructor"]:
            return False
        
        # Always summarize templates and macros
        if context_data.symbol_kind in ["template", "macro"]:
            return True
        
        # Skip if already has clear brief description
        content = context_data.primary_content
        if content.startswith("@brief") and len(content.split('\n')[0]) < 80:
            return False
            
        return True

    def _estimate_tokens(self, prompt: str, response: str) -> int:
        """Rough token estimation (1 token ≈ 4 characters)
        
        Args:
            prompt: Input prompt text
            response: Response text
            
        Returns:
            Estimated total tokens used
        """
        total_chars = len(prompt) + len(response)
        return total_chars // 4