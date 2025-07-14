import React, { useState } from "react";
import axios from "axios";

const Compare: React.FC = () => {
  const [reference, setReference] = useState<File | null>(null);
  const [candidate, setCandidate] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string>("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!reference || !candidate) {
      setError("Please upload both documents.");
      return;
    }
    setLoading(true);
    setError("");
    setResult(null);
    const formData = new FormData();
    formData.append("reference", reference);
    formData.append("candidate", candidate);
    try {
      const res = await axios.post("/compare-documents", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setResult(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.error || "Error comparing documents.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 700, margin: "auto", padding: 32 }}>
      <h2>Semantic Document Comparison</h2>
      <form onSubmit={handleSubmit}>
        <div>
          <label>Reference Document:</label>
          <input type="file" accept=".pdf,.docx,.txt,.png,.jpg" onChange={e => setReference(e.target.files?.[0] || null)} />
        </div>
        <div>
          <label>Candidate Document:</label>
          <input type="file" accept=".pdf,.docx,.txt,.png,.jpg" onChange={e => setCandidate(e.target.files?.[0] || null)} />
        </div>
        <button type="submit" disabled={loading}>Compare</button>
      </form>
      {loading && <div>Processing...</div>}
      {error && <div style={{ color: "red" }}>{error}</div>}
      {result && (
        <div>
          <h3>Compliance Score: {result.complianceScore}%</h3>
          <table border={1} cellPadding={8} style={{ width: "100%", marginTop: 16 }}>
            <thead>
              <tr>
                <th>Section ID</th>
                <th>Match Score</th>
                <th>Status</th>
                <th>Explanation</th>
              </tr>
            </thead>
            <tbody>
              {result.sections.map((sec: any) => (
                <tr key={sec.sectionId}>
                  <td>{sec.sectionId}</td>
                  <td>{sec.matchScore}</td>
                  <td>{sec.status}</td>
                  <td>{sec.explanation}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default Compare;
