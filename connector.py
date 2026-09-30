import traceback
from .connector_factory import get_domain_name_from_connector
from ..utils import safe_post_query_request, extract_text_from_file
from ..hls_constants import get_constant
from .base_connector import BaseConnector
from typing import Dict, List, Any, Tuple, Optional
from werkzeug.utils import secure_filename
from fastapi.responses import Response, FileResponse
from fastapi import HTTPException
import tempfile
import os
import requests
from sqlalchemy.ext.asyncio import AsyncSession
import json
import pymupdf
import re
from common.observability_utils.logging import get_logger
logger = get_logger(__name__)


class VeevaPromomatsConnector(BaseConnector):
    """Veeva Promomats document connector implementation."""
    
    def __init__(self):
        
        self.base_url = None
        self.username = None
        self.password = None
        self.connector_type = "promomats"

        

    async def load_credentials(self, db: AsyncSession):
        self.base_url = str(await get_constant("promomats_base_url", db) or "").strip()
        self.username = str(await get_constant("promomats_username", db) or "").strip()
        self.password = str(await get_constant("promomats_password", db) or "").strip()
    
    def validate_credentials(self) -> bool:
        print(f"🔑 Validating Veeva Promomats credentials: base_url={self.base_url}, username={self.username}, password={'***' if self.password else None}")
        return all([self.base_url, self.username, self.password])
    
    def authenticate(self) -> Tuple[str, Dict[str, str], str]:
        # Veeva promomats auth logic
        if not self.validate_credentials():
            raise ValueError(
                "Missing Veeva Promomats credentials. "
                "Please set promomats_base_url, promomats_username, and promomats_password in hls_constants table."
            )

        print(f"🔐 Authenticating with Veeva Promomats: {self.base_url}")
        auth_url = f"{self.base_url}/auth"
        data={"username": self.username, "password": self.password}
        auth_resp = safe_post_query_request(url=auth_url, data=data, timeout=15, verify=False)

        if auth_resp.status_code != 200:
            raise Exception(f"Authentication failed: {auth_resp.text}")

        resp_data = auth_resp.json()
        if resp_data.get("responseStatus") != "SUCCESS":
            error_msg = resp_data.get("responseMessage", "Unknown error")
            raise Exception(f"Authentication failed for Veeva Promomats: {error_msg}")

        session_id = resp_data.get("sessionId")
        if not session_id:
            raise Exception("No sessionId returned from Veeva Promomats")

        print(f"✅ Authenticated successfully. Session ID: {session_id[:10]}...")

        headers = {
            "Authorization": session_id,
            "Accept": "application/json"
        }

        return session_id, headers, self.base_url
    
    def _execute_query(self, payload: Dict[str, str]) -> Dict[str, Any]:
        """
        Execute a query against Veeva Promomats API.
        
        Args:
            payload (Dict[str, str]): Query payload with 'q' key containing the SQL query
            
        Returns:
            Dict: Response data from the API
            
        Raises:
            ValueError: If credentials are missing
            Exception: If authentication or query execution fails
        """
        if not self.validate_credentials():
            raise ValueError(
                "Missing Veeva Promomats credentials. "
                "Please set PROMOMATS_BASE_URL, PROMOMATS_USERNAME, and PROMOMATS_PASSWORD."
            )

        # Authenticate
        session_id, headers, base_url = self.authenticate()
        headers["X-VaultAPI-DescribeQuery"] = 'true'
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        # Execute query
        url = f'{base_url}/query'
        resp = safe_post_query_request(url=url, headers=headers, data=payload)

        if resp.status_code != 200:
            raise Exception(f"Query execution failed: {resp.text}")
        
        data = resp.json()

        # print(f"Query executed with STATUS: {data.get("responseStatus")}, {data.get("warnings", "")}, {data.get("messages", "")}, {data.get("errors", "")}")
        # if data.get("responseStatus") != "SUCCESS" and data.get("responseStatus") != "WARNING":
        #     raise Exception(f"Query failed: {data.get('errors', [])}")
        print(f"Query executed with STATUS: {data.get('responseStatus')}, {data.get('warnings', '')}, {data.get('messages', '')}, {data.get('errors', '')}")
        
        return data
    
    def get_documents(self, limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        """
        Retrieve a paginated list of documents from Veeva Promomats.
        
        Args:
            limit (int): Maximum documents per request (default 200)
            offset (int): Starting position for pagination (default 0)
            
        Returns:
            Dict with 'files' key containing document list, or 'error' key on failure
        """
        try:
            if not self.validate_credentials():
                raise ValueError(
                    "Missing Veeva Promomats credentials. "
                    "Please set PROMOMATS_BASE_URL, PROMOMATS_USERNAME, and PROMOMATS_PASSWORD."
                )

            # Authenticate
            session_id, headers, base_url = self.authenticate()

            # Retrieve all documents with pagination
            all_documents = []
            start = offset
            version_scope = "latest"

            while True:
                url = (
                    f"{base_url}/objects/documents?"
                    f"start={start}&limit={limit}&versionscope={version_scope}"
                )
                resp = requests.get(url, headers=headers, timeout=30, verify=False)

                if resp.status_code != 200:
                    raise Exception(f"Document retrieval failed: {resp.text}")

                data = resp.json()
                documents = data.get("documents", [])
                all_documents.extend(documents)


                print(f"📦 Retrieved {len(documents)} documents (offset={start})")

                # Stop when fewer than limit
                if len(documents) < limit:
                    break

                start += limit

            # Transform to frontend-friendly structure
            output = []
            for doc_entry in all_documents:
                doc = doc_entry.get("document", {})
                if not doc:
                    continue


                doc_status = doc.get("status__v", "Unknown")

                output.append({
                    "id": f"{doc.get('id')}_{doc.get('version_id')}",
                    "document_id": doc.get("id"),
                    "version_id": doc.get("version_id"),
                    "name": doc.get("name__v"),
                    "filename": doc.get("filename__v"),
                    "status": doc_status,
                    "brand_id": doc.get("product__v"),
                    "domain": get_domain_name_from_connector(self.connector_type),
                })

            print(f"✅ Total {len(output)} documents retrieved successfully.")
            return {"files": output}
        
        except Exception as e:
            print(f"❌ Error fetching documents Veeva Promats: {traceback.format_exc()}")
            return {
                "success": False,
                "error": str(e),
                "status_code": 500
            }
    
    def download_document(self, document_id: str) -> Response:
        """
        Download a document from Veeva and return as Flask Response.

        POST /hls_platform/view_veeva_files
        Body: { "veeva_file_id": "<doc_id>_<version_id>" }
        Authenticates with Veeva, downloads the document file to a temp directory,
        sends it to the frontend, then removes the temp file.

        
        Args:
            document_id (str): Veeva document ID
            
        Returns:
            Flask Response object with file content for download
            
        Raises:
            Exception: If download or file operations fail
        """
        temp_dir = None
        try:
            if not document_id:
                raise HTTPException(status_code=400, detail="document_id is required")

            print(f"👁️ Downloading Veeva document: {document_id}")

            # Authenticate
            session_id, headers, base_url = self.authenticate()

            # Download file bytes
            download_url = f"{base_url}/objects/documents/{document_id}/file"
            resp = requests.get(
                download_url,
                headers=headers,
                stream=True,
                timeout=60,
                verify=False
            )

            if resp.status_code != 200:
                print(f"❌ Veeva download failed ({resp.status_code}): {resp.text}")
                raise Exception(f"Failed to fetch file from Veeva: {resp.text}")

            # Detect content type and filename
            content_type = resp.headers.get("Content-Type", "application/pdf")
            content_disposition = resp.headers.get("Content-Disposition", "")
            print(f"📄 Content-Type: {content_type}, Content-Disposition: {content_disposition}")

            filename = f"document_{document_id}.pdf"

            if "filename=" in content_disposition:
                try:
                    filename = content_disposition.split("filename=")[-1].strip().strip('"')
                except Exception:
                    pass

            safe_filename = secure_filename(filename.replace("/", "_").replace("\\", "_"))

            # Save to temp directory
            temp_dir = tempfile.mkdtemp()
            file_path = os.path.join(temp_dir, safe_filename)

            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            print(f"✅ Downloaded {os.path.getsize(file_path)} bytes")

            return FileResponse(file_path, filename=safe_filename)

        except Exception as e:
            print(f"❌ Error downloading document: {str(e)}")
            if temp_dir and os.path.exists(temp_dir):
                import shutil
                shutil.rmtree(temp_dir)
            raise
    
    def download_file(self, document_id: str, file_name: str | None, output_path: str) -> str:
        """
        Download a document from Veeva Promomats and save to local file system.
        
        Args:
            document_id (str): Veeva document ID
            file_name (str | None): Desired filename for saving (optional)
            output_path (str): Directory where file will be saved
            
        Returns:
            str: Path to the saved file
            
        Raises:
            Exception: If download fails
        """
        try:
            print(f"⬇️  Downloading Veeva document: {document_id}")

            # Authenticate
            session_id, headers, base_url = self.authenticate()

            # Download file bytes
            download_url = f"{base_url}/objects/documents/{document_id}/file"
            resp = requests.get(
                download_url,
                headers=headers,
                stream=True,
                timeout=60,
                verify=False
            )

            if resp.status_code != 200:
                raise Exception(f"Failed to fetch file from Veeva: {resp.text}")

            # Extract filename
            content_disposition = resp.headers.get("Content-Disposition", "")
            filename = file_name

            if not filename and "filename*=" in content_disposition:
                try:
                    filename = content_disposition.split("filename*=")[-1]
                    filename = filename.replace("UTF-8''", "").strip().strip('"')
                except Exception:
                    pass

            if not filename and "filename=" in content_disposition:
                try:
                    filename = content_disposition.split("filename=")[-1].strip().strip('"')
                except Exception:
                    pass

            if not filename:
                filename = f"document_{document_id}"

            safe_filename = secure_filename(filename.replace("/", "_").replace("\\", "_"))

            # Ensure output directory exists
            os.makedirs(output_path, exist_ok=True)

            # Save file
            file_path = os.path.join(output_path, safe_filename)

            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            file_size = os.path.getsize(file_path)
            print(f"✅ Saved to {file_path} ({file_size} bytes)")

            return file_path

        except Exception as e:
            print(f"❌ Error downloading file: {str(e)}")
            raise

    def download_viewable_rendition(
        self,
        document_id: str,
        file_name: str | None,
        output_path: str
    ) -> str:
        """
        Download the viewable rendition of a Veeva document.

        Args:
            document_id (str): Veeva document ID
            file_name (str | None): Desired filename (optional)
            output_path (str): Directory where file will be saved

        Returns:
            str: Path to saved rendition file

        Raises:
            Exception: If download fails
        """
        try:
            print(f"⬇️ Downloading viewable rendition for document: {document_id}")

            # Authenticate
            session_id, headers, base_url = self.authenticate()

            # Download rendition
            rendition_url = (
                f"{base_url}/objects/documents/"
                f"{document_id}/renditions/viewable_rendition__v"
            )

            resp = requests.get(
                rendition_url,
                headers=headers,
                stream=True,
                timeout=60,
                verify=False,
            )

            if resp.status_code != 200:
                raise Exception(
                    f"Failed to download rendition. "
                    f"Status={resp.status_code}, Response={resp.text}"
                )

            # Determine filename
            content_disposition = resp.headers.get("Content-Disposition", "")

            filename = file_name or f"viewable_rendition_{document_id}.pdf"

            if "filename=" in content_disposition:
                try:
                    filename = (
                        content_disposition.split("filename=")[-1]
                        .strip()
                        .strip('"')
                    )
                except Exception:
                    pass

            safe_filename = secure_filename(
                filename.replace("/", "_").replace("\\", "_")
            )

            # Ensure directory exists
            os.makedirs(output_path, exist_ok=True)

            # Save file
            file_path = os.path.join(output_path, safe_filename)

            with open(file_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            file_size = os.path.getsize(file_path)

            print(
                f"✅ Viewable rendition saved to "
                f"{file_path} ({file_size} bytes)"
            )

            return file_path

        except Exception as e:
            print(f"❌ Error downloading rendition: {e}")
            raise

    def check_if_autotag_processed(self, document_id: str) -> bool:
        """
        Check if the document has already been processed for auto-tagging.

        Args:
            document_id (str): Veeva document ID
        """
        try:
            logger.info("🔍 Checking if document %s has been auto-tag processed", document_id)

            payload = {
                "q": f"SELECT mlr_autotag_processed__c FROM documents WHERE id = '{document_id}'"
            }
            response = self._execute_query(payload)
            skip = response.get("data", [{}])[0].get("mlr_autotag_processed__c", False)

            if skip:
                logger.info("⏭️ Document %s has already been auto-tag processed (mlr_autotag_processed__c=True)", document_id)
            else:
                logger.info("✅ Document %s has NOT been auto-tag processed (mlr_autotag_processed__c=False)", document_id)

            return skip

        except Exception as e:
            logger.error("❌ Error checking auto-tag status for document %s: %s", document_id, e)
            raise

    def download_annotations_file(
        self,
        document_id: str,
        file_name: str | None,
        output_path: str
    ) -> str:
        """
        Download the annotations file of a Veeva document.

        Args:
            document_id (str): Veeva document ID
            file_name (str | None): Desired filename (optional)
            output_path (str): Directory where file will be saved

        Returns:
            str: Path to saved annotations file

        Raises:
            Exception: If download fails
        """
        try:
            logger.info("⬇️ Downloading annotations file for document: %s", document_id)

            # Authenticate
            session_id, headers, base_url = self.authenticate()

            # hardcoded new base url for newer endpoint
            base_url = "https://partnersi-coeus-bridgeview-promomats.veevavault.com/api/v26.2"

            # Download annotations
            annotations_url = (
                f"{base_url}/objects/documents/"
                f"{document_id}/annotations/file"
            )

            resp = requests.get(
                annotations_url,
                headers=headers,
                stream=True,
                timeout=60,
                verify=False,
            )

            if resp.status_code != 200:
                raise Exception(
                    f"Failed to download annotations. "
                    f"Status={resp.status_code}, Response={resp.text}"
                )

            # Determine filename
            content_disposition = resp.headers.get("Content-Disposition", "")

            filename = file_name or f"annotations_{document_id}.pdf"

            if "filename=" in content_disposition:
                try:
                    filename = (
                        content_disposition.split("filename=")[-1]
                        .strip()
                        .strip('"')
                    )
                except Exception:
                    pass

            safe_filename = secure_filename(
                filename.replace("/", "_").replace("\\", "_")
            )

            # Ensure directory exists
            os.makedirs(output_path, exist_ok=True)

            # Save file
            file_path = os.path.join(output_path, safe_filename)

            with open(file_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            file_size = os.path.getsize(file_path)

            logger.info(
                "✅ Annotations file saved to %s (%d bytes)", file_path, file_size
            )

            return file_path

        except Exception as e:
            logger.error("❌ Error downloading annotations: %s", e)
            raise

    @staticmethod
    def _detect_source_extension(file_path: str) -> str:
        """Real file type from the bytes. Unknown types fall back to '.html'
        (the previous behaviour for non-office sources)."""
        import zipfile

        try:
            with open(file_path, "rb") as f:
                head = f.read(4096)
            if head.startswith(b"%PDF"):
                return ".pdf"
            if head.startswith(b"PK"):
                try:
                    with zipfile.ZipFile(file_path) as z:
                        names = z.namelist()
                    if any(n.startswith("word/") for n in names):
                        return ".docx"
                    if any(n.startswith("ppt/") for n in names):
                        return ".pptx"
                except zipfile.BadZipFile:
                    pass
                return ".html"
            lowered = head.lstrip().lower()
            if lowered.startswith(b"<!doctype html") or b"<html" in lowered:
                return ".html"
        except Exception as e:
            print(f"⚠️ Could not sniff {file_path}: {e}")
        return ".html"

    def _rename_to_document_id(self, file_path: str, doc_id) -> str:
        """Rename a downloaded source file to '<document_id><real ext>'."""
        ext = self._detect_source_extension(file_path)
        new_path = os.path.join(os.path.dirname(file_path), f"{doc_id}{ext}")
        if os.path.abspath(new_path) != os.path.abspath(file_path):
            os.replace(file_path, new_path)
        print(f"📄 Source file for {doc_id} saved as {os.path.basename(new_path)}")
        return new_path

    async def download_all_files(self, db: AsyncSession, file_entries: List[Any], output_folder: str) -> Tuple[List[str], str]:
        """
        Download all files from a list of file entries and extract text content.
        
        This method downloads multiple documents from Veeva Vault, extracts text
        from each file, and returns both the list of downloaded file paths and
        the combined text content from all documents.
        
        Args:
            file_entries (List[Any]): List of file entry dicts or document IDs.
                Each entry can be a dict with keys: 'id', 'document_id', 'version_id', 'filename'
                or a simple string document ID.
            output_folder (str): Directory where files will be saved
            
        Returns:
            Tuple containing:
            - downloaded_files (List[str]): List of downloaded file names
            - combined_content (str): Combined extracted text from all documents
            
        Raises:
            Exception: If all files fail to download or no files were successfully processed
        """
        try:
            print(f"📥 [START] download_all_files() - Downloading {len(file_entries)} documents...")
            
            # Ensure output folder exists
            os.makedirs(output_folder, exist_ok=True)
            downloaded_files = []
            downloaded_source_files = []
            combined_content = ""
            failed_count = 0
            
            for idx, file_entry in enumerate(file_entries, start=1):
                try:
                    # Extract document_id from entry (handle both dict and string formats)
                    if isinstance(file_entry, dict):
                        doc_id = file_entry.get('id') or file_entry.get('document_id')
                        filename = file_entry.get('filename')
                    else:
                        doc_id = file_entry
                        filename = None
                    
                    if not doc_id:
                        print(f"⚠️  [{idx}/{len(file_entries)}] Missing document_id, skipping...")
                        failed_count += 1
                        continue

                    # Use provided filename or fallback
                    if not filename:
                        filename = file_entry.get('filename') if isinstance(file_entry, dict) else None
                    
                    print(f"⬇️ Downloading Document ID: {doc_id}")
                    
                    # Download file using download_file method
                    file_path = self.download_file(doc_id, filename, output_folder)
                    file_path = file_path.replace("\\\\", "\\")

                    if isinstance(file_entry, dict) and file_entry.get("save_as_document_id"):
                        file_path = self._rename_to_document_id(file_path, doc_id)
                    safe_filename = os.path.basename(file_path)
                    
                     # 🎯 FIX: Skip text extraction entirely for HTML files to keep logs clean
                    if safe_filename.lower().endswith((".html", ".htm")):
                        downloaded_files.append(safe_filename)
                        downloaded_source_files.append(
                            {
                                "document_id": doc_id,
                                "filename": safe_filename,
                                "file_path": file_path,
                            }
                        )

                        continue
                    
                    # Extract text from the downloaded file
                    try:
                        extracted_text = extract_text_from_file(file_path)
                        combined_content += f"\n\n--- Document: {safe_filename} ---\n{extracted_text}"
                        downloaded_files.append(safe_filename)
                        downloaded_source_files.append(
                                                    {
                                                        "document_id": doc_id,
                                                        "filename": safe_filename,
                                                        "file_path": file_path,
                                                    }
                                                )
                        
                        print(f"✅ [{idx}/{len(file_entries)}] Processed: {safe_filename}")
                    except Exception as extract_err:
                        print(file_path, safe_filename)
                        print(f"⚠️  [{idx}/{len(file_entries)}] Error extracting text from {safe_filename}: {str(extract_err)}")
                        # Still count as downloaded even if text extraction fails
                        downloaded_files.append(safe_filename)
                        downloaded_source_files.append(
                                                    {
                                                        "document_id": doc_id,
                                                        "filename": safe_filename,
                                                        "file_path": file_path,
                                                    }
                                                )
                        continue
                    
                except Exception as e:
                    failed_count += 1
                    print(f"❌ [{idx}/{len(file_entries)}] Error processing file entry: {str(e)}")
                    continue

            
            if not downloaded_files:
                raise Exception(f"No documents were successfully downloaded (failed: {failed_count})")
            
            print(f"✅ [END] download_all_files() - Downloaded {len(downloaded_files)} files, Failed: {failed_count}")
            print(f"📝 Combined content length: {len(combined_content)} characters")
            
            return(
                downloaded_files,
                downloaded_source_files,
                combined_content,
            )
        
        except Exception as e:
            print(f"❌ Error in download_all_files: {str(e)}")
            raise
    
    def get_connector_type(self) -> str:
        return self.connector_type
    
    def get_claims(self, brand_id: str = '') -> Dict[str, Any]:
        """
        Fetch claims from Veeva Promomats.
        Args:            brand_id (str): Optional brand ID to filter claims. If empty, retrieves claims for all brands.
        Returns:        Dict with 'claims' key containing list of claims, or 'error' key on failure
        """
        try:
            payload = {
                "q": "SELECT id, name__v, product__v, state__v, match_text__sys from annotation_keywords__sys" + ("" if brand_id == "" else f" where product__v = '{brand_id}'")
            }
            data = self._execute_query(payload)
            claims = data.get("data", [])   
            print(f"📋 Retrieved {len(claims)} claims")

            # Transform to frontend-friendly structure
            output_claims = []
            for claim in claims:
                output_claims.append({
                    "id": claim.get("id"),
                    "name": claim.get("name__v"),
                    "brand_id": claim.get("product__v"),
                    "state__v": claim.get("state__v", "Unknown"),
                    "match_text": claim.get("match_text__sys"),
                })

            print(f"✅ Total {len(output_claims)} claims retrieved successfully.")
            return {"claims": output_claims}
        except Exception as e:
            print(f"❌ Error fetching claims: {str(e)}")
            return {"error": str(e)}

    def update_claim(self, claim_id: str, match_text: str) -> Dict[str, Any]:
        """
        Update a claim text in Veeva Promomats.
        """

        try:
            print(f"✏️ Updating claim: {claim_id}")

            _, headers, base_url = self.authenticate()

            update_url = (
                f"{base_url}/vobjects/annotation_keywords__sys/{claim_id}"
            )

            payload = {
                "match_text__sys": match_text
            }

            print("UPDATE URL =", update_url)
            print("PAYLOAD =", payload)

            resp = requests.put(
                update_url,
                headers=headers,
                data=payload,
                verify=False,
                timeout=30
            )

            print("STATUS =", resp.status_code)
            print("RESPONSE =", resp.text)

            if resp.status_code != 200:
                raise Exception(resp.text)

            return {
                "success": True,
                "claim_id": claim_id,
                "response": resp.json()
            }

        except Exception as e:
            print(f"❌ Error updating claim: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }

    def get_link_target_id(self, claim_id: str) -> str:
        _, headers, base_url = self.authenticate()

        query = f"""
        SELECT id, annotation_keyword__v, link_target__v
        FROM annotation_keyword_targets__sys
        WHERE annotation_keyword__v = '{claim_id}'
        """

        response = requests.post(
            f"{base_url}/query",
            headers=headers,
            data={"q": query},
            verify=False,
        )

        response.raise_for_status()

        records = response.json().get("data", [])

        if not records:
            raise Exception(
                f"No annotation_keyword_targets__sys record found for claim {claim_id}"
            )

        return records[0]["link_target__v"]
    
    def create_new_claim(
    self,
    claim_text: str,
    object_type_id: str,
    product_id: str,
    country_id: str,
    source_approval_document: str = None,
    source_approval_document_unbound: str = None,
    source_text_asset: str = None,
    ):
        _, headers, base_url = self.authenticate()

        payload = {
            "match_text__sys": claim_text,
            "object_type__v": object_type_id,
            "product__v": product_id,
            "country__v": country_id,
        }

        if source_approval_document:
            payload["source_approval_document__v"] = source_approval_document

        if source_approval_document_unbound:
            payload["source_approval_document_unbound__v"] = (
                source_approval_document_unbound
            )

        if source_text_asset:
            payload["source_text_asset__v"] = source_text_asset
        
        print("\n==============================")
        print("CREATE NEW CLAIM")
        print("==============================")
        print("PAYLOAD =", payload)

        response = requests.post(
            f"{base_url}/vobjects/annotation_keywords__sys",
            headers=headers,
            data=payload,
            verify=False,
        )

        response.raise_for_status()

        data = response.json()
        
        print("RESPONSE =", data)

        new_claim_id = data.get("data", {}).get("id")

        if not new_claim_id:
            raise Exception(f"Claim creation failed: {data}")

        return new_claim_id

    def create_claim_relationship(
    self,
    claim_id: str,
    link_target_id: str,
    ):
        _, headers, base_url = self.authenticate()

        payload = {
            "annotation_keyword__v": claim_id,
            "link_target__v": link_target_id,
        }

        print("\n==============================")
        print("CREATE CLAIM RELATIONSHIP")
        print("==============================")
        print("PAYLOAD =", payload)
        
        response = requests.post(
            f"{base_url}/vobjects/annotation_keyword_targets__sys",
            headers=headers,
            data=payload,
            verify=False,
        )

        response.raise_for_status()

        data = response.json()
        print("RESPONSE =", data)

        if data.get("responseStatus") != "SUCCESS":
            raise Exception(
                f"Relationship creation failed: {data}"
            )

        return data
    
    def withdraw_claim(self, claim_id: str):
        _, headers, base_url = self.authenticate()

        action_name = (
            "Objectlifecyclestateuseraction."
            "annotation_keywords__sys."
            "approved_state__sys."
            "change_state_to_withdrawn_useraction__sys"
        )

        # 🎯 FIX: Force the layout pattern that you verified worked perfectly
        url = (
            f"{base_url}/vobjects/annotation_keywords__sys/"
            f"{claim_id}/actions/{action_name}"
        )

        # Duplicate headers safely and ensure Content-Type is bound
        headers_copy = headers.copy()
        headers_copy["Content-Type"] = "application/x-www-form-urlencoded"

        print(f"🚀 Sending Hardcoded Withdrawn Action to: {url}")
        response = requests.post(
            url,
            headers=headers_copy,
            verify=False,
            timeout=30
        )

        print(f"Status Code: {response.status_code}")
        try:
            print(response.json())
        except Exception:
            print(response.text)

        response.raise_for_status()
        return response.json()

    
    def approve_claim(self, claim_id: str):
        _, headers, base_url = self.authenticate()

        # Create a copy of the headers and explicitly pin the Content-Type
        headers_copy = headers.copy()
        headers_copy["Content-Type"] = "application/x-www-form-urlencoded"

        # ----------------------------------------------------------------------
        # STEP 1: Draft -> In Review
        # ----------------------------------------------------------------------
        action_name_1 = (
            "Objectlifecyclestateuseraction."
            "annotation_keywords__sys."
            "draft_state__sys."
            "change_state_to_in_review_useraction__sys"
        )

        url_1 = (
            f"{base_url}/vobjects/annotation_keywords__sys/"
            f"{claim_id}/actions/{action_name_1}"
        )

        print(f"🚀 Sending Transition: Draft -> In Review for claim {claim_id}")
        response_1 = requests.post(
            url_1,
            headers=headers_copy,
            data={},
            verify=False,
            timeout=30
        )

        response_1.raise_for_status()
        print("✅ DRAFT -> IN REVIEW =", response_1.json())

        if response_1.json().get("responseStatus") != "SUCCESS":
            raise Exception(f"Draft -> In Review failed: {response_1.text}")


        # ----------------------------------------------------------------------
        # STEP 2: In Review -> Approved
        # ----------------------------------------------------------------------
        action_name_2 = (
            "Objectlifecyclestateuseraction."
            "annotation_keywords__sys."
            "in_review_state__sys."
            "change_state_to_approved_useraction__sys"
        )

        url_2 = (
            f"{base_url}/vobjects/annotation_keywords__sys/"
            f"{claim_id}/actions/{action_name_2}"
        )

        print(f"🚀 Sending Transition: In Review -> Approved for claim {claim_id}")
        response_2 = requests.post(
            url_2,
            headers=headers_copy,
            data={},
            verify=False,
            timeout=30
        )

        response_2.raise_for_status()
        print("✅ IN REVIEW -> APPROVED =", response_2.json())

        if response_2.json().get("responseStatus") != "SUCCESS":
            raise Exception(f"In Review -> Approved failed: {response_2.text}")

        return response_2.json()

    
    def get_claim_details(
    self,
    claim_id: str,
    ):
        _, headers, base_url = self.authenticate()

        url = (
            f"{base_url}/vobjects/annotation_keywords__sys/"
            f"{claim_id}"
        )

        response = requests.get(
            url,
            headers=headers,
            verify=False,
        )

        response.raise_for_status()

        return response.json().get("data", {})
    
    def get_claim_name(
    self,
    claim_id: str,
    ):
        claim_data = self.get_claim_details(claim_id)

        return claim_data.get("name__v"," ")

    def get_relevant_docs_for_claim(self, claim_id: str) -> Dict[str, Any]:
        """
        Fetch ONLY the latest unique version of documents using a claim from Veeva Promomats.
        """
        try:
            print(f"🔍 Fetching relevant documents for claim: {claim_id}")

            # Authenticate
            _, headers, base_url = self.authenticate()

            endpoint = "/ui/suggestedlink/whereused"
            params = {
                "repoId": 247178,
                "claimId": claim_id,
                "pageNumber": 0,
                "columnKey": "lastUsedDate",
                "sortDirection": "desc", # Keeps latest dates at the top
                "searchValue": ""
            }

            vault_root = base_url.split("/api")[0]
            url = f"{vault_root}{endpoint}"

            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=30,
                verify=False
            )

            if resp.status_code != 200:
                raise Exception(f"Where-used request failed: {resp.text}")

            data = resp.json()
            if data.get("status") != "SUCCESS":
                raise Exception(f"Veeva returned error: {data}")

            grid_data = json.loads(data.get("payload", {}).get("gridData", "{}"))
            records = grid_data.get("listOfRecords", [])

            print(f"📄 Raw response returned {len(records)} historical usage records.")

            output_docs = []
            seen_document_numbers = set()

            for record in records:
                doc_info = record.get("docName", {})
                doc_id_major_minor = doc_info.get("docIdMajorMinor", "")
                document_number = record.get("docNumber") # e.g. 'MAT-0214'

                # Skip if we already captured the latest version of this document number
                if document_number in seen_document_numbers:
                    print(f"⏭️ Skipping historical duplicate version of document: {document_number} ({doc_id_major_minor})")
                    continue

                document_id = None
                major_version = None
                minor_version = None

                if doc_id_major_minor:
                    parts = doc_id_major_minor.split("/")
                    document_id = parts[0]
                    
                    # 🎯 FIX: Instead of taking parts[1] (which is stale V0.1), look at the record's current major/minor context 
                    # if available in docName metadata container, or fall back to splitting the version chain.
                    # To capture the absolute latest existing version info (V0.70) out of the record:
                    version_text = doc_info.get("versionLabel", "") # e.g., "0.70"
                    if version_text and "." in version_text:
                        v_parts = version_text.split(".")
                        major_version = v_parts[0]
                        minor_version = v_parts[1]
                    elif len(parts) >= 3:
                        major_version = parts[1]
                        minor_version = parts[2]
                    else:
                        major_version = "0"
                        minor_version = "0"

                # Mark this document number as captured so no older versions pass through
                seen_document_numbers.add(document_number)

                print(f"✅ CAPTURING EXISTING VERSION -> Doc: {document_number} | Version: {document_id}/{major_version}/{minor_version}")

                output_docs.append({
                    "document_id": document_id,
                    "major_version": major_version,
                    "minor_version": minor_version,
                    "document_name": doc_info.get("docName"),
                    "document_version": f"{document_id}/{major_version}/{minor_version}",
                    "document_number": document_number,
                    "document_status": record.get("docStatus"),
                    "last_used_date": record.get("lastUsedDate"),
                    "accepted_count": record.get("acceptedCount", 0),
                    "pending_count": record.get("pendingCount", 0),
                    "manual_count": record.get("manualCount", 0),
                    "auto_count": record.get("autoCount", 0),
                    "suggested_count": record.get("suggestedCount", 0),
                    "rejected_count": record.get("rejectedCount", 0),
                })

            print(f"✅ Successfully filtered down to {len(output_docs)} unique latest documents.")
            return {
                "claim_id": claim_id,
                "documents": output_docs
            }

        except Exception as e:
            print(f"❌ Error fetching relevant documents for claim: {str(e)}")
            return {
                "error": str(e)
            }





    async def download_relevant_docs_for_claim(
    self,
    db: AsyncSession,
    claim_id: str,
    ):
        """
        Download all documents related to a claim.
        """

        docs_response = self.get_relevant_docs_for_claim(claim_id)

        documents = docs_response.get("documents", [])

        if not documents:
            return {
                "success": False,
                "claim_id": claim_id,
                "message": "No relevant documents found"
            }

        # ------------------------------------------------------------------
        # REMOVE DUPLICATE DOCUMENTS
        # Keep only latest occurrence per document name
        # ------------------------------------------------------------------
        unique_documents = []
        seen_document_ids = set()

        for doc in documents:
            document_id = doc.get("document_id")

            if document_id not in seen_document_ids:
                seen_document_ids.add(document_id)
                unique_documents.append(doc)

        documents = unique_documents

        print(f"📄 Unique documents after dedupe: {len(documents)}")

        for doc in documents:
            print(
                f"✅ KEPT -> "
                f"{doc.get('document_id')} | "
                f"{doc.get('document_name')}"
            )

        base_dir = os.path.dirname(
            os.path.dirname(__file__)
        )

        html_folder = os.path.join(
            base_dir,
            "claim_docs_download_html",
            claim_id
        )

        pdf_folder = os.path.join(
            base_dir,
            "claim_docs_download_pdf",
            claim_id
        )

        os.makedirs(html_folder, exist_ok=True)
        os.makedirs(pdf_folder, exist_ok=True)

        print(f"📄 HTML Folder: {html_folder}")
        print(f"📕 PDF Folder: {pdf_folder}")

        # 🎯 FIXED: Re-map the file entry dictionary layout before passing to download paths
        # This overrides the human-readable document text title name parameter, forcing it to save as '{document_id}.html'
        # Do NOT force ".html" here: the source can be a docx / pptx / pdf.
        # Let Vault's Content-Disposition give the real name, then
        # download_all_files() renames it to '{document_id}<real extension>'.
        id_mapped_documents = []
        for doc in documents:
            doc_copy = doc.copy()
            doc_id = doc.get("document_id")
            if doc_id:
                doc_copy["filename"] = None
                doc_copy["save_as_document_id"] = True
            id_mapped_documents.append(doc_copy)

        # Download HTML/source files
        (downloaded_files,downloaded_source_files,combined_content,) = await self.download_all_files(
            db=db,
            file_entries=id_mapped_documents,
            output_folder=html_folder
        )


                # Download Annotated PDFs
        print("📥 Downloading annotated PDFs...")

        pdf_downloaded_files = []

        for document in documents:
            print(
                "DOWNLOADING =>",
                document.get("document_name"),
                "|",
                document.get("doc_id_major_minor")
            )
            try:
                document_id = document.get("document_id")

                if not document_id:
                    continue

                print(
                    f"📕 Downloading annotated PDF for document: "
                    f"{document_id}"
                )

                session_id, headers, base_url = self.authenticate()

                major_version = document.get("major_version", "0")
                minor_version = document.get("minor_version", "0")

                annotation_url = (
                    f"{base_url}/objects/documents/"
                    f"{document_id}/versions/{major_version}/{minor_version}/annotations"
                )

                print(
                    f"PDF VERSION => {document_id}/{major_version}/{minor_version}"
                )

                response = requests.get(
                    annotation_url,
                    headers=headers,
                    verify=False,
                    timeout=60,
                )

                response.raise_for_status()

                document_name = document.get("document_name", str(document_id))

                safe_document_name = re.sub(
                    r'[<>:"/\\|?*]',
                    "_",
                    document_name
                )

                pdf_path = os.path.join(
                    pdf_folder,
                    f"annotated_{safe_document_name}.pdf"
                )

                with open(pdf_path, "wb") as pdf_file:
                    pdf_file.write(response.content)

                print(
                    f"✅ Annotated PDF saved: {pdf_path}"
                )

                pdf_downloaded_files.append(
                    {
                        "document_id": document_id,
                        "document_name": document.get("document_name"),
                        "pdf_path": pdf_path,
                    }
                )
                
            except Exception as e:
                print(
                    f"❌ Failed to download annotated PDF "
                    f"for document {document_id}: {e}"
                )
        print("\nSOURCE FILES:")
        for item in downloaded_source_files:
            print(item)

        return {
            "success": True,
            "claim_id": claim_id,
            "html_folder": html_folder,
            "pdf_folder": pdf_folder,
            "downloaded_files": downloaded_files,
            "source_files": downloaded_source_files,
            "pdf_downloaded_files": pdf_downloaded_files,
            "combined_content": combined_content
        }
    
        
    def get_brands(self) -> Dict[str, Any]:
        try:
            payload = {
                "q": "SELECT id, name__v FROM product__v"
            }

            print(f"🔍 Fetching brands from Veeva Promomats...")
            data = self._execute_query(payload)
            brands = data.get("data", [])   
 
            print(f"📋 Retrieved {len(brands)} brands")

            # Transform to frontend-friendly structure
            brand_map = {}
            for brand in brands:
                brand_map[brand.get("id")] = brand.get("name__v")

            print(f"✅ Total {len(brand_map)} brands retrieved successfully from Promomats.")
            return brand_map
        except Exception as e:
            print(f"❌ Error fetching brands: {str(e)}")
            return {"error": str(e)}
        
    def get_brand_name(self, brand_id: str) -> str:
        brands_data = self.get_brands()
        return brands_data.get(brand_id, "Unknown Brand")
    
    def get_brand_id(self, brand_name: str) -> str:
        brands_data = self.get_brands()
        for b_id, b_name in brands_data.items():
            if b_name.lower() == brand_name.lower():
                return b_id
        return None
    
    def get_images_metadata(self, brand_id: str = '') -> Dict[str, Any]:
        """
        Fetch metadata for approved images from Veeva Promomats.
        Args:            brand_id (str): Optional brand ID to filter images. If empty, retrieves images for all brands.
        Returns:        Dict with 'images' key containing list of image metadata, or 'error' key on failure 
        """
        try:
            payload = {
                "q": f"SELECT id, major_version_number__v, minor_version_number__v, name__v, product__v, title__v from documents where type__v='Component' and subtype__v='Image' and status__v = 'Approved for Use'" + ("" if brand_id == "" else f" and product__v = '{brand_id}'")
            }
            data = self._execute_query(payload)
            image_data = data.get("data", [])   
            print(f"📋 Retrieved {len(image_data)} images")

            # Transform to frontend-friendly structure
            output_images = []
            for image in image_data:
                output_images.append({
                    "id": image.get("id"),
                    "document_version_id__sys": str(image.get("id")) + "_" + str(image.get("major_version_number__v")) + "_" + str(image.get("minor_version_number__v")),
                    "name": image.get("name__v"),
                    "brand_id": image.get("product__v"),
                    "description": image.get("title__v", ""),
                    "status__v": "Approved for use"
                })

            logger.info(f"Sample retrieved image metadata: {output_images[-1]}")
            logger.info(f"✅ Total {len(output_images)} images retrieved successfully.")
            return {"images": output_images}
        except Exception as e:
            logger.error(f"❌ Error fetching images: {str(e)}")
            return {"error": str(e)}
        
    def get_image_url(self, document_id: str) -> str:
        try:
            session_id, headers, base_url = self.authenticate()
            download_url = f"{base_url}/objects/documents/{document_id}/file"
            return download_url
        except Exception as e:
            print(f"❌ Error getting image URL: {str(e)}")
            return None
        
    def get_image_as_base64(self, document_id: str) -> str:
        try:
            session_id, headers, base_url = self.authenticate()
            download_url = self.get_image_url(document_id)
            resp = requests.get(download_url, headers=headers, timeout=30, verify=False)

            if resp.status_code != 200:
                raise Exception(f"Failed to fetch image from {self.connector_type}: {resp.text}")

            import base64
            return base64.b64encode(resp.content).decode('utf-8')
        except Exception as e:
            print(f"❌ Error getting image as base64: {str(e)}")
            return None

    def get_image_urls(self, document_ids: List[str]) -> Dict[str, Optional[str]]:
        """
        Bulk version of `get_image_url`.
        Returns a mapping of document_id -> download_url or None on error.
        """
        results: Dict[str, Optional[str]] = {}
        try:
            # authenticate once
            session_id, headers, base_url = self.authenticate()
            for doc_id in document_ids or []:
                try:
                    if not doc_id:
                        results[doc_id] = None
                        continue
                    results[doc_id] = f"{base_url}/objects/documents/{doc_id}/file"
                except Exception as e:
                    logger.error(f"❌ Error getting image URL for {doc_id}: {e}")
                    results[doc_id] = None
        except Exception as e:
            logger.error(f"❌ Error getting image URLs: {str(e)}")
            # If authentication failed return all None
            for doc_id in document_ids or []:
                results[doc_id] = None

        return results

    def get_images_as_base64(self, document_ids: List[str]) -> Dict[str, Optional[str]]:
        """
        Bulk version of `get_image_as_base64`.
        Returns a mapping of document_id -> base64 string or None on error.
        """
        results: Dict[str, Optional[str]] = {}
        try:
            session_id, headers, base_url = self.authenticate()
            import base64

            for doc_id in document_ids or []:
                try:
                    if not doc_id:
                        results[doc_id] = None
                        continue

                    download_url = f"{base_url}/objects/documents/{doc_id}/file"
                    resp = requests.get(download_url, headers=headers, timeout=30, verify=False)

                    if resp.status_code != 200:
                        logger.error(f"⚠️ Failed to fetch image {doc_id}: {resp.status_code}")
                        results[doc_id] = None
                        continue

                    logger.info(f"Downloaded document id: {doc_id} as base64")

                    results[doc_id] = base64.b64encode(resp.content).decode('utf-8')
                except Exception as e:
                    logger.error(f"❌ Error getting image as base64 for {doc_id}: {e}")
                    results[doc_id] = None

        except Exception as e:
            logger.error(f"❌ Error in bulk get_images_as_base64: {str(e)}")
            for doc_id in document_ids or []:
                results[doc_id] = None

        return results

    def get_document_names(self, document_version_ids: List[str]) -> Dict[str, str]:
        """
        Fetch document names for a list of document version IDs.
        
        Args:
            document_version_ids (List[str]): List of document version IDs in format "document_id_version_id"
            
        Returns:
            Dict[str, str]: Mapping of document_id to document_name
            
        Raises:
            Exception: If query execution fails
        """
        try:
            if not document_version_ids:
                logger.error("⚠️ No document version IDs provided")
                return {}
            
            # Extract unique document IDs from document_version_ids
            document_ids = set()
            for doc_version_id in document_version_ids:
                if doc_version_id:
                    # Format is typically "id_version_id", extract just the id part
                    doc_id = doc_version_id.split('_')[0]
                    document_ids.add(doc_id)
            
            if not document_ids:
                logger.error("⚠️ No valid document IDs extracted from document_version_ids")
                return {}
            
            logger.info(f"🔍 Fetching document names for {len(document_ids)} documents from Veeva Promomats...")
            
            # Build SQL query with IN clause
            id_list = "', '".join(document_ids)
            payload = {
                "q": f"SELECT id, name__v FROM documents WHERE id CONTAINS ('{id_list}')"
            }

            data = self._execute_query(payload)
            documents = data.get("data", [])
            
            # Create mapping of document_id to document_name
            document_names: Dict[str, str] = {}
            for doc in documents:
                doc_id = doc.get("id")
                name = doc.get("name__v")
                if doc_id and name:
                    document_names[str(doc_id)] = name
            
            logger.info("Retrieved %s document names successfully.", len(document_names))
            return document_names
            
        except Exception as e:
            logger.error("❌ Error fetching document names: %s", str(e))
            return {}

    def get_not_mlr_tagged_documents(self) -> Dict[str, Any]:
        """
        Retrieve approved documents not tagged by mlr and skip those where
        mlr_autotag_processed is set to 'true'.
        """
        try:
            if not self.validate_credentials():
                raise ValueError(
                    "Missing Veeva Promomats credentials. "
                    "Please set PROMOMATS_BASE_URL, PROMOMATS_USERNAME, and PROMOMATS_PASSWORD."
                )

            payload = {
                "q": f"SELECT id, name__v, filename__v, product__v, status__v, version_id, mlr_autotag_processed__c FROM documents"
            }

            data = self._execute_query(payload)
            documents = data.get("data", [])

            output = []
            skipped_count = 0

            for doc in documents:
                raw_value = str(doc.get("mlr_autotag_processed__c", "false")).strip().lower()
                is_processed = raw_value == "true"

                if is_processed:
                    skipped_count += 1
                    continue

                output.append({
                    "id": f"{doc.get('id')}_{doc.get('version_id')}",
                    "document_id": doc.get("id"),
                    "version_id": doc.get("version_id"),
                    "name": doc.get("name__v"),
                    "filename": doc.get("filename__v"),
                    "status": doc.get("status__v", "Unknown"),
                    "brand_id": doc.get("product__v"),
                    "domain": get_domain_name_from_connector(self.connector_type),
                })

            logger.info("✅ Total %s documents retrieved successfully.", len(output))
            return {
                "success": True,
                "files": output,
                "total_approved": len(documents),
                "skipped_count": skipped_count,
            }
        
        except Exception as e:
            logger.error("❌ Error fetching documents Veeva Promats: %s", str(e))
            return {
                "success": False,
                "error": str(e),
                "status_code": 500
            }

    def get_documents_for_brand(self, brand_id: str) -> Dict[str, Any]:
        """
        Retrieve approved documents for a specific brand and skip those where
        claim_processed__c is set to 'true'.
        """
        try:
            if not self.validate_credentials():
                raise ValueError(
                    "Missing Veeva Promomats credentials. "
                    "Please set PROMOMATS_BASE_URL, PROMOMATS_USERNAME, and PROMOMATS_PASSWORD."
                )

            if not brand_id:
                raise ValueError("brand_id is required")

            brand_id = str(brand_id).strip()

            payload = {
                "q": f"SELECT id, name__v, filename__v, product__v, status__v, version_id, claim_processed__c FROM documents WHERE (status__v = 'Approved' OR status__v = 'Approved for Use' OR status__v = 'Approved for Distribution') AND product__v = '{brand_id}'"
            }

            data = self._execute_query(payload)
            documents = data.get("data", [])

            output = []
            skipped_count = 0

            for doc in documents:
                raw_value = doc.get("claim_processed__c")
                is_processed = False

                if isinstance(raw_value, str):
                    is_processed = raw_value.strip().lower() == "true"
                elif raw_value is None:
                    is_processed = False

                filename = doc.get("filename__v").lower()
                image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".svg"}
                is_image = os.path.splitext(filename)[1] in image_extensions

                if is_processed:
                    skipped_count += 1
                    continue

                output.append({
                    "id": f"{doc.get('id')}_{doc.get('version_id')}",
                    "document_id": doc.get("id"),
                    "version_id": doc.get("version_id"),
                    "name": doc.get("name__v"),
                    "filename": doc.get("filename__v"),
                    "status": doc.get("status__v", "Unknown"),
                    "brand_id": brand_id,
                    "domain": get_domain_name_from_connector(self.connector_type),
                    "is_image": is_image,
                })

            return {
                "success": True,
                "files": output,
                "total_approved": len(documents),
                "skipped_count": skipped_count,
            }

        except Exception as e:
            print(f"❌ Error fetching brand-specific documents from Veeva Promomats: {traceback.format_exc()}")
            return {
                "success": False,
                "error": str(e),
                "status_code": 500,
            }
    
    def get_document_compare_url(
        self,
        document_id: str,
        current_major: int = None,
        current_minor: int = None,
        compare_major: int = None,
        compare_minor: int = None,
        **kwargs  # 🚀 ADDED: Gracefully intercept old keyword naming parameters
    ):
        """
        Generate Veeva compare URL using explicit, verified version parameters.
        Safely bridges version gaps while supporting backwards compatibility.
        """
        # ------------------------------------------------------------------
        # 🎯 BACKWARDS COMPATIBILITY LAYER: Intercept old naming formats
        # ------------------------------------------------------------------
        # If legacy code passes major_version / minor_version, treat them as the current version
        if current_major is None and "major_version" in kwargs:
            current_major = kwargs["major_version"]
        if current_minor is None and "minor_version" in kwargs:
            current_minor = kwargs["minor_version"]

        # Dynamic fallback calculation logic if comparison keys weren't passed explicitly
        if compare_major is None:
            compare_major = current_major if current_major is not None else 0
            
        if compare_minor is None:
            # Safe boundary deduction fallback if minor_version came from old format
            compare_minor = (
                current_minor - 1
                if current_minor and current_minor > 1
                else 1
            )

        # ------------------------------------------------------------------
        # URL GENERATION LIFECYCLE
        # ------------------------------------------------------------------
        vault_root = self.base_url.split("/api")[0]

        view_url = (
            f"{vault_root}"
            f"/ui/#doc_info/"
            f"{document_id}/"
            f"{current_major}/"
            f"{current_minor}"
            f"?cma={compare_major}"
            f"&cmi={compare_minor}"
        )

        print("\n==============================")
        print("FIXED COMPATIBLE COMPARE URL")
        print("==============================")
        print(f"DOCUMENT ID       = {document_id}")
        print(f"CURRENT VERSION   = V{current_major}.{current_minor}")
        print(f"COMPARING AGAINST = V{compare_major}.{compare_minor}")
        print(f"COMPARE URL       = {view_url}")

        return {
            "success": True,
            "view_url": view_url,
        }
    
    def find_latest_existing_predecessor(self, document_id: str, current_major: int, current_minor: int) -> Tuple[int, int]:
        """
        Queries Veeva Vault for all existing versions of a document.
        Traverses backwards from the current version to find the closest existing predecessor.
        """
        session_id, headers, base_url_raw = self.authenticate()
        
        # 🎯 FIX 1: Extract index 0 cleanly from the split string list to get a valid base root URL
        api_root = base_url_raw.split("/api")[0]
        base_url = f"{api_root}/api/v26.2"
        
        print("🔄 Initiating active pre-flight decrement loop validation scan...")
        
        scan_minor = int(current_minor) - 1
        scan_major = int(current_major)
        
        while scan_minor >= 0:
            if scan_major == 0 and scan_minor == 0:
                break
                
            # Endpoint url to verify existence of this specific individual version node
            check_url = f"{base_url}/objects/documents/{document_id}/versions/{scan_major}/{scan_minor}"
            try:
                check_resp = requests.get(check_url, headers=headers, verify=False, timeout=5)
                
                # 🎯 FIX 2: LOGIC INVERSION OVERHAUL
                # Status 200 means the version EXISTS in Veeva! Return it immediately!
                if check_resp.status_code == 200:
                    c_data = check_resp.json()
                    if c_data.get("responseStatus") == "SUCCESS":
                        print(f"🎯 Pre-flight Scan Verified! Found closest existing version node: V{scan_major}.{scan_minor}")
                        return scan_major, scan_minor
                
                # If it returns 404 or any error, then it was deleted. Skip it!
                print(f"⏭️ Version V{scan_major}.{scan_minor} returned {check_resp.status_code} (Confirmed Deleted/Unavailable). Skipping...")
            except Exception as scan_err:
                print(f"⚠️ Error checking version node V{scan_major}.{scan_minor}: {scan_err}")
                
            scan_minor -= 1
            
        print("⚠️ Pre-flight loop scan hit structural boundary limit. Defaulting target to V0.1")
        return 0, 1
    
    def batch_delete_annotations(
    self,
    document_version_id: str,
        annotation_ids: list[str],
    ):
        """
        Deletes obsolete annotations using Veeva batch delete.

        document_version_id example:
        3192_0_2
        """

        try:

            if not annotation_ids:
                print("⚠️ No annotations supplied for deletion.")
                return None

            _, headers, base_url_raw = self.authenticate()

            api_root = base_url_raw.split("/api")[0]
            base_url = f"{api_root}/api/v26.2"

            url = (
                f"{base_url}/objects/documents/"
                f"annotations/batch?_method=DELETE"
            )

            payload = [
                {
                    "document_version_id__sys": document_version_id,
                    "id__sys": str(annotation_id),
                }
                for annotation_id in annotation_ids
            ]

            print("\n==============================")
            print("BATCH DELETE ANNOTATIONS")
            print("==============================")
            print(f"DOCUMENT VERSION = {document_version_id}")
            print(f"ANNOTATIONS = {annotation_ids}")

            response = requests.post(
                url,
                headers={
                    "Authorization": headers["Authorization"],
                    "Content-Type": "application/json",
                },
                json=payload,
                verify=False,
                timeout=60,
            )

            print(f"DELETE STATUS = {response.status_code}")

            try:
                print(json.dumps(response.json(), indent=2))
            except Exception:
                print(response.text)

            response.raise_for_status()

            return response.json()

        except Exception as e:
            print(f"❌ batch_delete_annotations failed: {e}")
            return None
