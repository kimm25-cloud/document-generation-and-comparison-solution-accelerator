import React, { useState } from "react";
import axios from "axios";

const Compare: React.FC = () => {
  const [reference, setReference] = useState<File | null>(null);
  const [candidate, setCandidate] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string>("");

  const downloadResults = (format: 'pdf') => {
    if (!result) return;

    const timestamp = new Date().toISOString().slice(0, 19).replace(/:/g, '-');
    const filename = `document-comparison-${timestamp}`;

    // Create a comprehensive PDF report
    const reportContent = `
      <html>
        <head>
          <title>Document Comparison Report</title>
          <style>
            body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }
            .header { text-align: center; margin-bottom: 30px; border-bottom: 2px solid #333; padding-bottom: 20px; }
            .score-box { background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; text-align: center; }
            .score-box h2 { color: #28a745; margin: 0; }
            table { width: 100%; border-collapse: collapse; margin: 20px 0; }
            th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
            th { background-color: #f2f2f2; font-weight: bold; }
            .status-pass { color: #28a745; font-weight: bold; }
            .status-warn { color: #ffc107; font-weight: bold; }
            .status-fail { color: #dc3545; font-weight: bold; }
            .summary { margin: 30px 0; padding: 20px; background: #e9ecef; border-radius: 8px; }
            .timestamp { color: #6c757d; font-size: 0.9em; margin-top: 20px; }
          </style>
        </head>
        <body>
          <div class="header">
            <h1>Document Comparison Report</h1>
            <p>Automated semantic analysis and compliance assessment</p>
          </div>
          
          <div class="score-box">
            <h2>Overall Compliance Score: ${result.complianceScore}%</h2>
            <p>Based on analysis of ${result.sections.length} sections</p>
          </div>
          
          <div class="summary">
            <h3>Summary</h3>
            <p><strong>Total Sections Analyzed:</strong> ${result.sections.length}</p>
            <p><strong>Sections Passed:</strong> ${result.sections.filter((s: any) => s.status === '✅').length}</p>
            <p><strong>Sections with Issues:</strong> ${result.sections.filter((s: any) => s.status === '⚠️').length}</p>
            <p><strong>Sections Failed:</strong> ${result.sections.filter((s: any) => s.status === '❌').length}</p>
          </div>
          
          <h3>Detailed Analysis</h3>
          <table>
            <thead>
              <tr>
                <th>Section</th>
                <th>Match Score</th>
                <th>Status</th>
                <th>Analysis</th>
              </tr>
            </thead>
            <tbody>
              ${result.sections.map((sec: any) => `
                <tr>
                  <td>${sec.sectionId}</td>
                  <td>${(sec.matchScore * 100).toFixed(1)}%</td>
                  <td class="${sec.status === '✅' ? 'status-pass' : sec.status === '⚠️' ? 'status-warn' : 'status-fail'}">${sec.status}</td>
                  <td>${sec.explanation}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
          
          <div class="timestamp">
            Report generated on: ${new Date().toLocaleString()}
          </div>
        </body>
      </html>
    `;
    
    // Create a new window and print it as PDF
    const printWindow = window.open('', '_blank');
    if (printWindow) {
      printWindow.document.write(reportContent);
      printWindow.document.close();
      printWindow.focus();
      setTimeout(() => {
        printWindow.print();
        printWindow.close();
      }, 250);
    }
  };

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
      <h2>Document Comparison</h2>
      <form onSubmit={handleSubmit} style={{ marginBottom: 32 }}>
        <div style={{ marginBottom: 16 }}>
          <label style={{ display: "block", marginBottom: 8, fontWeight: "bold" }}>Original Document:</label>
          <input 
            type="file" 
            accept=".pdf,.docx,.txt,.png,.jpg" 
            onChange={e => setReference(e.target.files?.[0] || null)}
            style={{ width: "100%", padding: 8, border: "1px solid #ccc", borderRadius: 4 }}
          />
        </div>
        <div style={{ marginBottom: 16 }}>
          <label style={{ display: "block", marginBottom: 8, fontWeight: "bold" }}>Updated Document:</label>
          <input 
            type="file" 
            accept=".pdf,.docx,.txt,.png,.jpg" 
            onChange={e => setCandidate(e.target.files?.[0] || null)}
            style={{ width: "100%", padding: 8, border: "1px solid #ccc", borderRadius: 4 }}
          />
        </div>
        <button 
          type="submit" 
          disabled={loading || !reference || !candidate}
          style={{ 
            padding: "12px 24px", 
            backgroundColor: loading || !reference || !candidate ? "#ccc" : "#007bff", 
            color: "white", 
            border: "none", 
            borderRadius: 4, 
            cursor: loading || !reference || !candidate ? "not-allowed" : "pointer",
            fontSize: "16px"
          }}
        >
          {loading ? "Analyzing..." : "Compare Documents"}
        </button>
      </form>
      {loading && <div>Analyzing documents...</div>}
      {error && <div style={{ color: "red" }}>{error}</div>}
      {result && (
        <div style={{ marginTop: 32 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <h3>Comparison Results</h3>
            <div>
              <button 
                onClick={() => downloadResults('pdf')}
                style={{ 
                  padding: "8px 16px", 
                  backgroundColor: "#dc3545", 
                  color: "white", 
                  border: "none", 
                  borderRadius: 4, 
                  cursor: "pointer",
                  fontSize: "14px"
                }}
              >
                Download PDF Report
              </button>
            </div>
          </div>
          <div style={{ marginBottom: 16, padding: 16, backgroundColor: "#f5f5f5", borderRadius: 8 }}>
            <strong>Overall Compliance Score: {result.complianceScore}%</strong>
          </div>
          <table border={1} cellPadding={8} style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ backgroundColor: "#f0f0f0" }}>
                <th>Section</th>
                <th>Match Score</th>
                <th>Status</th>
                <th>Analysis</th>
              </tr>
            </thead>
            <tbody>
              {result.sections.map((sec: any) => (
                <tr key={sec.sectionId}>
                  <td>{sec.sectionId}</td>
                  <td>{(sec.matchScore * 100).toFixed(1)}%</td>
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
