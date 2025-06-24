"""
Google Gemini 2.5 Flash provider for fast C++ code summarization
"""

import logging
import json
from typing import Optional, Dict
from langchain_google_genai import ChatGoogleGenerativeAI
from ..llm_provider import (LLMProvider, SummaryRequest, SummaryResponse, ContextData, 
                           CallAnalysisRequest, CallAnalysisResponse, CallPattern,
                           InheritanceAnalysisRequest, InheritanceAnalysisResponse, InheritancePattern)

logger = logging.getLogger(__name__)


class GeminiFlashProvider(LLMProvider):
    """Google Gemini 2.5 Flash provider for fast summarization"""
    
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self.llm = ChatGoogleGenerativeAI(
            model=f"models/{model}",
            google_api_key=api_key,
            temperature=0.1,  # Low temperature for consistent summaries
            max_tokens=200,   # Cap output length
            # No thinking_budget - we want speed over deep reasoning
        )
    
    async def summarize(self, request: SummaryRequest) -> Optional[SummaryResponse]:
        """Summarize C++ documentation using Gemini Flash"""
        
        # Only summarize if content is substantial enough
        if len(request.content) < 100:
            return None
            
        prompt = self._build_prompt(request)
        
        try:
            response = await self.llm.ainvoke(prompt)
            return SummaryResponse(
                summary=response.content.strip(),
                tokens_used=self._estimate_tokens(prompt, response.content),
                provider=f"gemini-2.5-flash",
                context_level=request.context_level
            )
        except Exception as e:
            logger.warning(f"Gemini summarization failed: {e}")
            return None
    
    async def summarize_with_context(self, context_data: ContextData) -> Optional[SummaryResponse]:
        """Summarize with context-aware prompts"""
        
        prompt = self._build_context_aware_prompt(context_data)
        
        try:
            response = await self.llm.ainvoke(prompt)
            return SummaryResponse(
                summary=response.content.strip(),
                tokens_used=self._estimate_tokens(prompt, response.content),
                provider=f"gemini-2.5-flash-{context_data.context_level}",
                context_level=context_data.context_level
            )
        except Exception as e:
            logger.warning(f"Gemini summarization failed: {e}")
            return None
    
    def _build_prompt(self, request: SummaryRequest) -> str:
        """Build an optimized prompt for fast C++ documentation summarization"""
        return f"""Summarize this C++ {request.symbol_kind} documentation in 1-2 clear sentences.
Focus on WHAT it does, not implementation details.

Symbol: {request.symbol_name}
Type: {request.symbol_kind}

Documentation:
{request.content}

Summary:"""

    def _build_context_aware_prompt(self, context_data: ContextData) -> str:
        """Build prompts that adapt to context level and symbol type"""
        
        if context_data.context_level == "minimal":
            return self._build_minimal_prompt(context_data)
        elif context_data.context_level == "local":
            return self._build_local_prompt(context_data)
        elif context_data.context_level == "full":
            return self._build_full_prompt(context_data)
        else:
            return self._build_minimal_prompt(context_data)
    
    def _build_minimal_prompt(self, context_data: ContextData) -> str:
        """Fast prompt for documentation-only context"""
        symbol_prompts = {
            "function": "Explain what this C++ function does in one clear sentence:",
            "class": "Describe this C++ class and its primary purpose:",
            "template": "Explain this C++ template in simple terms:",
            "macro": "Explain what this C++ macro expands to and why it's used:",
            "variable": "Describe this C++ variable and its purpose:",
        }
        
        base_prompt = symbol_prompts.get(context_data.symbol_kind, "Summarize this C++ documentation:")
        
        return f"""{base_prompt}

Symbol: {context_data.symbol_name}
Type: {context_data.symbol_kind}

Documentation:
{context_data.primary_content}

Summary (1-2 sentences):"""

    def _build_local_prompt(self, context_data: ContextData) -> str:
        """Enhanced prompt with surrounding code context"""
        return f"""Analyze this C++ {context_data.symbol_kind} with its surrounding code context.

Symbol: {context_data.symbol_name}
Location: {context_data.source}

Documentation:
{context_data.primary_content}

Surrounding Code Context:
{context_data.surrounding_code}

{f"Class Context: {context_data.class_context}" if context_data.class_context else ""}

Provide a clear 2-3 sentence summary explaining:
1. What this {context_data.symbol_kind} does
2. How it fits in the surrounding code context

Summary:"""

    def _build_full_prompt(self, context_data: ContextData) -> str:
        """Rich prompt with full file and dependency context"""
        headers_context = ""
        if context_data.related_headers:
            headers_context = f"""

Related Headers:
{chr(10).join(context_data.related_headers)}"""

        imports_context = ""
        if context_data.imports:
            imports_context = f"""

Imports:
{chr(10).join(context_data.imports)}"""

        return f"""Analyze this C++ {context_data.symbol_kind} within its complete codebase context.

Symbol: {context_data.symbol_name}
Context Source: {context_data.source}

Full File Content:
{context_data.primary_content[:10000]}  # Truncate if too long

{headers_context}

{imports_context}

Provide a comprehensive 3-4 sentence summary that explains:
1. What this {context_data.symbol_kind} does
2. Its role in the overall file/module
3. Key dependencies or relationships
4. Any important implementation details

Summary:"""

    def is_available(self) -> bool:
        """Check if the provider is properly configured"""
        return bool(self.api_key)
    
    async def analyze_call_hierarchy(self, request: CallAnalysisRequest, 
                                   context_data: Optional[Dict] = None) -> Optional[CallAnalysisResponse]:
        """Analyze call hierarchy with AI insights"""
        
        # Only analyze if we have substantial call data
        if not request.calls or len(request.calls) == 0:
            return None
            
        prompt = self._build_call_analysis_prompt(request, context_data)
        
        try:
            response = await self.llm.ainvoke(prompt)
            return self._parse_call_analysis_response(response.content, request)
        except Exception as e:
            logger.warning(f"Gemini call analysis failed: {e}")
            return None
    
    def _build_call_analysis_prompt(self, request: CallAnalysisRequest, 
                                   context_data: Optional[Dict] = None) -> str:
        """Build prompt for call hierarchy analysis"""
        
        # Format call hierarchy data
        calls_info = []
        for call in request.calls[:20]:  # Limit to prevent token explosion
            call_info = f"  - {call.get('name', 'unknown')} in {call.get('file', 'unknown')}:{call.get('line', 0)}"
            if call.get('detail'):
                call_info += f" ({call['detail']})"
            calls_info.append(call_info)
        
        calls_text = "\n".join(calls_info)
        
        if request.analysis_type == "incoming":
            return self._build_incoming_calls_prompt(request, calls_text)
        else:
            return self._build_outgoing_calls_prompt(request, calls_text)
    
    def _build_incoming_calls_prompt(self, request: CallAnalysisRequest, calls_text: str) -> str:
        """Build prompt for incoming calls analysis"""
        return f"""Analyze the incoming calls to this C++ function and provide structured insights.

TARGET FUNCTION: {request.target_function}
LOCATION: {request.target_file}:{request.target_line}

INCOMING CALLS ({len(request.calls)} total):
{calls_text}

Please provide a JSON response with this structure:
{{
  "analysis_summary": "Brief overview of this function's role and usage patterns",
  "patterns": [
    {{
      "pattern_type": "validation|error_handling|initialization|computation|io|logging|cleanup",
      "description": "What this pattern represents",
      "call_count": 3,
      "confidence": 0.8
    }}
  ],
  "architectural_insights": "How this function fits in the codebase architecture",
  "data_flow_analysis": "What data typically flows to/from this function",
  "performance_notes": "Any performance considerations or bottlenecks"
}}

Focus on practical insights that help developers understand the function's role and impact."""

    def _build_outgoing_calls_prompt(self, request: CallAnalysisRequest, calls_text: str) -> str:
        """Build prompt for outgoing calls analysis"""
        return f"""Analyze the outgoing calls from this C++ function and provide structured insights.

SOURCE FUNCTION: {request.target_function}
LOCATION: {request.target_file}:{request.target_line}

OUTGOING CALLS ({len(request.calls)} total):
{calls_text}

Please provide a JSON response with this structure:
{{
  "analysis_summary": "Brief overview of what this function accomplishes through its calls",
  "execution_flow": "Logical sequence and flow of the function calls",
  "dependencies": {{
    "core_utilities": ["list of utility functions called"],
    "io_operations": ["list of I/O related calls"],
    "external_apis": ["list of external API calls"]
  }},
  "architectural_insights": "How this function orchestrates its dependencies",
  "data_flow_analysis": "How data flows through the call chain",
  "performance_notes": "Bottlenecks, optimization opportunities, or critical paths"
}}

Focus on understanding the function's internal logic and dependency patterns."""

    def _parse_call_analysis_response(self, response_content: str, 
                                    request: CallAnalysisRequest) -> CallAnalysisResponse:
        """Parse AI response into structured call analysis"""
        
        try:
            # Try to extract JSON from response
            response_content = response_content.strip()
            if response_content.startswith("```json"):
                response_content = response_content[7:]
            if response_content.endswith("```"):
                response_content = response_content[:-3]
            
            data = json.loads(response_content)
            
            # Extract patterns for incoming calls
            patterns = []
            if "patterns" in data:
                for pattern_data in data["patterns"]:
                    pattern = CallPattern(
                        pattern_type=pattern_data.get("pattern_type", "unknown"),
                        calls=[],  # We don't map specific calls back to patterns for now
                        description=pattern_data.get("description", ""),
                        confidence=pattern_data.get("confidence", 0.5)
                    )
                    patterns.append(pattern)
            
            return CallAnalysisResponse(
                analysis_summary=data.get("analysis_summary", "No summary available"),
                patterns=patterns,
                architectural_insights=data.get("architectural_insights", ""),
                data_flow_analysis=data.get("data_flow_analysis", ""),
                performance_notes=data.get("performance_notes", ""),
                tokens_used=self._estimate_tokens(str(request.calls), response_content),
                provider=f"gemini-2.5-flash-{request.analysis_type}"
            )
            
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse call analysis response: {e}")
            # Fallback to text-based response
            return CallAnalysisResponse(
                analysis_summary=response_content[:500],  # Truncate if too long
                patterns=[],
                architectural_insights="",
                data_flow_analysis="",
                performance_notes="",
                tokens_used=self._estimate_tokens(str(request.calls), response_content),
                provider=f"gemini-2.5-flash-{request.analysis_type}"
            )
    
    async def analyze_inheritance(self, request: InheritanceAnalysisRequest,
                                context_data: Optional[Dict] = None) -> Optional[InheritanceAnalysisResponse]:
        """Analyze inheritance hierarchy with AI insights"""
        
        try:
            prompt = self._build_inheritance_analysis_prompt(request, context_data)
            response = await self.llm.ainvoke(prompt)
            response_content = response.content
            
            return self._parse_inheritance_analysis_response(response_content, request)
        
        except Exception as e:
            logger.error(f"Inheritance analysis failed: {e}")
            return None
    
    def _build_inheritance_analysis_prompt(self, request: InheritanceAnalysisRequest,
                                         context_data: Optional[Dict] = None) -> str:
        """Build prompt for inheritance analysis"""
        
        context_info = ""
        if context_data:
            context_info = f"\n\nAdditional context:\n{context_data}"
        
        if request.analysis_type == "supertypes":
            direction = "base classes/supertypes"
            relationship = "inherits from"
        else:
            direction = "derived classes/subtypes"
            relationship = "is inherited by"
        
        analysis_detail = ""
        if request.analysis_level == "detailed":
            analysis_detail = """
            Please provide detailed analysis including:
            - Specific design patterns used (Strategy, Template Method, Observer, etc.)
            - Inheritance depth and complexity analysis
            - Potential refactoring opportunities
            - Interface segregation opportunities
            - Liskov Substitution Principle adherence
            """
        
        prompt = f"""Analyze this C++ inheritance hierarchy.

Target type: {request.target_type} at {request.target_file}:{request.target_line}:{request.target_column}
Analysis type: {direction}
Analysis level: {request.analysis_level}

Related types ({direction}):
{self._format_types_for_analysis(request.types)}

{analysis_detail}

Please analyze the inheritance relationships and provide insights in JSON format:
{{
    "analysis_summary": "Brief overview of the inheritance structure",
    "patterns": [
        {{
            "pattern_name": "Name of inheritance pattern (e.g., 'Abstract Base Class', 'Polymorphic Hierarchy')",
            "types": ["list", "of", "type", "names"],
            "description": "Description of this pattern",
            "confidence": 0.9
        }}
    ],
    "architectural_insights": "High-level architectural observations about the inheritance design",
    "design_patterns": "Specific design patterns identified (Strategy, Factory, etc.)",
    "refactoring_suggestions": "Suggestions for improving the inheritance hierarchy"
}}

Focus on:
- Object-oriented design principles
- Inheritance depth and complexity
- Virtual function usage patterns
- Interface design quality
- Potential code smells or improvements{context_info}"""

        return prompt
    
    def _format_types_for_analysis(self, types: list) -> str:
        """Format types for analysis prompt"""
        if not types:
            return "No related types found"
        
        formatted = []
        for i, type_info in enumerate(types[:10], 1):  # Limit to 10 for prompt size
            name = type_info.get("name", "unknown")
            file = type_info.get("file", "")
            line = type_info.get("line", 0)
            detail = type_info.get("detail", "")
            
            formatted.append(f"{i}. {name} ({file}:{line}) - {detail}")
        
        return "\n".join(formatted)
    
    def _parse_inheritance_analysis_response(self, response_content: str,
                                           request: InheritanceAnalysisRequest) -> InheritanceAnalysisResponse:
        """Parse inheritance analysis response from LLM"""
        
        try:
            # Try to extract JSON from response
            import json
            import re
            
            json_match = re.search(r'\{.*\}', response_content, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                data = json.loads(json_str)
            else:
                raise ValueError("No JSON found in response")
            
            # Parse patterns
            patterns = []
            if "patterns" in data:
                for pattern_data in data["patterns"]:
                    pattern = InheritancePattern(
                        pattern_name=pattern_data.get("pattern_name", "unknown"),
                        types=pattern_data.get("types", []),
                        description=pattern_data.get("description", ""),
                        confidence=pattern_data.get("confidence", 0.5)
                    )
                    patterns.append(pattern)
            
            return InheritanceAnalysisResponse(
                analysis_summary=data.get("analysis_summary", "No summary available"),
                patterns=patterns,
                architectural_insights=data.get("architectural_insights", ""),
                design_patterns=data.get("design_patterns", ""),
                refactoring_suggestions=data.get("refactoring_suggestions", ""),
                tokens_used=self._estimate_tokens(str(request.types), response_content),
                provider=f"gemini-2.5-flash-{request.analysis_type}"
            )
            
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse inheritance analysis response: {e}")
            # Fallback to text-based response
            return InheritanceAnalysisResponse(
                analysis_summary=response_content[:500],  # Truncate if too long
                patterns=[],
                architectural_insights="",
                design_patterns="",
                refactoring_suggestions="",
                tokens_used=self._estimate_tokens(str(request.types), response_content),
                provider=f"gemini-2.5-flash-{request.analysis_type}"
            )