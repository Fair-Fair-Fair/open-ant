"""Built-in tools for agent capabilities."""
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from ant.tools.base import tool

if TYPE_CHECKING:
    from ant.core.agent import AgentSession


@tool(name="read",
      description="Read the content of a text file",
      parameters={
          "type": "object",
          "properties": {
              "path": {
                  "type": "string",
                  "description": "The path to the text file to read"
              }
          },
          "required": ["path"]
      })
async def read_file(path: str, session: "AgentSession") -> str:
    """Read and return the contents of a file at the given path."""
    session.shared_context.sandbox.path.validate_read(path)
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except PermissionError:
        return f"Error: Permission denied reading: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error reading file: {e}"


@tool(name="write",
      description="write content to a file",
      parameters={
          "type": "object",
          "properties": {
              "path": {
                  "type": "string",
                  "description": "The path to the text file to write"
              },
              "content": {
                  "type": "string",
                  "description": "The content to write to the file"
              }
          },
          "required": ["path", "content"]
      })
async def write_file(path: str, content: str, session: "AgentSession") -> str:
    """Write content to a file at the given path."""
    session.shared_context.sandbox.path.validate_write(path)
    try:
        Path(path).write_text(content)
        return f"Successfully wrote to: {path}"
    except PermissionError:
        return f"Error: Permission denied writing to: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error writing file: {e}"


@tool(name="edit",
      description="Edit a file by replacing a string with new content",
      parameters={
          "type": "object",
          "properties": {
              "path": {
                  "type": "string",
                  "description": "The path to the text file to edit"
              },
              "old_string": {
                  "type": "string",
                  "description": "The string to be replaced"
              },
              "new_string": {
                  "type": "string",
                  "description": "The new string to insert"
              }
          },
          "required": ["path", "old_string", "new_string"]
      })
async def edit_file(path: str, old_string: str, new_string: str, session: "AgentSession") -> str:
    """Edit a file by replacing a string with new content."""
    session.shared_context.sandbox.path.validate_write(path)
    try:
        content = Path(path).read_text()
        if old_string not in content:
            return f"Error: '{old_string}' not found in file: {path}"
        new_content = content.replace(old_string, new_string)
        Path(path).write_text(new_content)
        return f"Successfully edited: {path}"
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied editing: {path}"
    except Exception as e:
        return f"Error editing file: {e}"


@tool(name="bash",
      description="Execute a bash shell command",
      parameters={
          "type": "object",
          "properties": {
              "command": {
                  "type": "string",
                  "description": "The bash command to execute"
              }
          },
          "required": ["command"]
      })
async def bash(command: str, session: "AgentSession") -> str:
    """Execute a bash command and return the output."""
    sb = session.shared_context.sandbox.command
    sb.validate_command(command)

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=sb.working_dir,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=sb.timeout
        )
        output = stdout.decode() if stdout else ""
        error = stderr.decode() if stderr else ""

        output = sb.validate_output(output)
        error = sb.validate_output(error)

        if output and error:
            return f"{output}\n{error}"
        return output or error or "Command completed with no output"
    except asyncio.TimeoutError:
        return f"Error: Command timed out after {sb.timeout} seconds"
    except Exception as e:
        return f"Error executing command: {e}"
